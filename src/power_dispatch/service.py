"""电价、燃料库存、送出线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import (
    SystemClock,
    load_zone,
    parse_naive_local,
    parse_utc,
    resolve_local,
    utc_text,
    tz_version,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    DeliveryWindowSpec,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    decimal_value,
    identifier,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {
        "nomination.write",
        "allocation.run",
        "transfer.write",
        "inventory.write",
        "delivery.write",
        "delivery.read",
    },
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read", "delivery.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("电价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准电价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("燃料批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("燃料批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可送电版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("燃料批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("燃料批次与送出线路起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("燃料库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def _transfer_destination(self, transfer_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT t.*,r.destination_id,r.product FROM transfers t "
            "JOIN nominations n ON n.nomination_id=t.nomination_id "
            "JOIN routes r ON r.route_id=n.route_id WHERE t.transfer_id=?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise NotFound("送电单不存在")
        return row

    def _resolve_window_spec(self, facility: sqlite3.Row, spec: DeliveryWindowSpec) -> dict[str, Any]:
        """按目的地设施的 IANA 日历解析 ETA，并冻结 tzdata 版本与截止时刻。"""
        zone = load_zone(facility["timezone"], "facility.timezone")
        eta_instant, eta_kind = resolve_local(parse_naive_local(spec.eta_local, "eta_local"), zone)
        deadline = eta_instant + timedelta(hours=spec.grace_hours)
        return {
            "facility_id": facility["facility_id"],
            "timezone": facility["timezone"],
            "tz_version": tz_version(),
            "eta_local": spec.eta_local,
            "eta_kind": eta_kind,
            "eta_at": utc_text(eta_instant),
            "deadline_at": utc_text(deadline),
            "grace_hours": spec.grace_hours,
        }

    def open_delivery_window(self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "delivery.write")
        spec = DeliveryWindowSpec.from_dict(raw)
        transfer = self._transfer_destination(transfer_id)
        facility = self.connection.execute(
            "SELECT * FROM facilities WHERE facility_id=?", (transfer["destination_id"],)
        ).fetchone()
        if facility is None:  # pragma: no cover - 外键已保证
            raise NotFound("目的地设施不存在")
        if self.connection.execute(
            "SELECT 1 FROM delivery_windows WHERE transfer_id=? AND batch_no=?",
            (transfer_id, spec.batch_no),
        ).fetchone() is not None:
            raise Conflict("该批次的到厂窗口已经存在")
        resolved = self._resolve_window_spec(facility, spec)
        expected = quantize_volume(Decimal(transfer["expected_delivered_mwh"]))
        with transaction(self.connection, immediate=True):
            window_id = self._insert_window_row(
                transfer_id, spec.batch_no, resolved, expected, Decimal("0"), "open", actor_id
            )
            self._audit("delivery_window", str(window_id), "delivery.window_opened", actor_id, {
                "transfer_id": transfer_id,
                "batch_no": spec.batch_no,
                "timezone": resolved["timezone"],
                "tz_version": resolved["tz_version"],
                "eta_local": resolved["eta_local"],
                "eta_kind": resolved["eta_kind"],
                "deadline_at": resolved["deadline_at"],
            })
        return self._window_dict(window_id)

    def _insert_window_row(
        self,
        transfer_id: str,
        batch_no: int,
        resolved: Mapping[str, Any],
        expected_mwh: Decimal,
        received_mwh: Decimal,
        state: str,
        actor_id: str,
        carried_from_window_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO delivery_windows(transfer_id,batch_no,facility_id,timezone,tz_version,"
            "eta_local,eta_kind,eta_at,deadline_at,grace_hours,expected_mwh,received_mwh,state,"
            "carried_from_window_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                transfer_id,
                batch_no,
                resolved["facility_id"],
                resolved["timezone"],
                resolved["tz_version"],
                resolved["eta_local"],
                resolved["eta_kind"],
                resolved["eta_at"],
                resolved["deadline_at"],
                resolved["grace_hours"],
                decimal_text(quantize_volume(expected_mwh)),
                decimal_text(quantize_volume(received_mwh)),
                state,
                carried_from_window_id,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _window_dict(self, window_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM delivery_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:  # pragma: no cover - 仅内部调用
            raise NotFound("到厂窗口不存在")
        result = dict(row)
        result["remaining_mwh"] = decimal_text(
            quantize_volume(Decimal(row["expected_mwh"]) - Decimal(row["received_mwh"]))
        )
        return result

    def _active_window(self, transfer_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM delivery_windows WHERE transfer_id=? AND state IN ('open','partial') "
            "ORDER BY batch_no LIMIT 1",
            (transfer_id,),
        ).fetchone()

    def _post_destination_inventory(
        self,
        *,
        transfer: sqlite3.Row,
        origin: sqlite3.Row,
        batch_no: int,
        quantity: Decimal,
        received_at: str,
        actor_id: str,
    ) -> str:
        """把签收数量登记到目的地设施库存；重复扫描不会重复增加库存。

        必须在已经打开的事务内调用。
        """
        lot_id = f"{transfer['transfer_id']}:b{batch_no}"
        amount = quantize_volume(quantity)
        existing = self.connection.execute(
            "SELECT quantity_mwh,available_mwh FROM inventory_lots WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if existing is None:
            self.connection.execute(
                "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,"
                "available_mwh,unit_cost_cny,received_at,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    lot_id,
                    transfer["destination_id"],
                    transfer["product"],
                    origin["grade"],
                    decimal_text(amount),
                    decimal_text(amount),
                    origin["unit_cost_cny"],
                    received_at,
                    actor_id,
                    self._now(),
                ),
            )
        else:
            total_quantity = quantize_volume(Decimal(existing["quantity_mwh"]) + amount)
            total_available = quantize_volume(Decimal(existing["available_mwh"]) + amount)
            self.connection.execute(
                "UPDATE inventory_lots SET quantity_mwh=?,available_mwh=?,revision=revision+1 "
                "WHERE lot_id=?",
                (decimal_text(total_quantity), decimal_text(total_available), lot_id),
            )
        return lot_id

    def record_delivery_scan(self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "delivery.write")
        try:
            scanned_at = parse_utc(str(raw.get("scanned_at") or ""), "scanned_at")
            quantity = decimal_value(raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001"))
            idempotency_key = identifier(raw.get("idempotency_key"), "idempotency_key")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        note = str(raw.get("note") or "")

        stored = self.connection.execute(
            "SELECT response_json FROM supply_idempotency WHERE scope='delivery_scan' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            return json.loads(stored["response_json"])

        scanned_text = utc_text(scanned_at)
        quantity_text = decimal_text(quantize_volume(quantity))
        transfer = self._transfer_destination(transfer_id)
        natural_duplicate = self.connection.execute(
            "SELECT scan_id FROM delivery_scans WHERE transfer_id=? AND scanned_at=? AND quantity_mwh=? "
            "AND classification IN ('on_time','late') ORDER BY scan_id LIMIT 1",
            (transfer_id, scanned_text, quantity_text),
        ).fetchone()
        if natural_duplicate is not None:
            return self._record_duplicate_scan(
                actor_id, transfer_id, scanned_at, quantity, idempotency_key,
                int(natural_duplicate["scan_id"]), note,
            )

        active = self._active_window(transfer_id)
        if active is None:
            raise InvalidState("到厂窗口已全部关闭，不能再登记签收")

        remaining = quantize_volume(Decimal(active["expected_mwh"]) - Decimal(active["received_mwh"]))
        if quantity > remaining:
            raise Conflict(f"签收数量超过批次待收 {decimal_text(remaining)}")

        is_late = scanned_at > parse_utc(active["deadline_at"])
        # 只允许从原始批次滚动一次；结转批继承同一冻结窗口，继续吸收后续迟到货物。
        rollover = is_late and active["carried_from_window_id"] is None
        origin = self.connection.execute(
            "SELECT product,grade,unit_cost_cny FROM inventory_lots WHERE lot_id=?",
            (transfer["inventory_lot_id"],),
        ).fetchone()
        if origin is None:  # pragma: no cover - 外键已保证
            raise NotFound("源燃料批次不存在")

        with transaction(self.connection, immediate=True):
            judged_window_id = int(active["window_id"])
            judged_batch_no = int(active["batch_no"])
            if rollover:
                # 原批次只冻结此前已准时签收的数量并以“结转关闭”归档；新增数量结转到
                # 新批次，新批次继承发运时冻结的同一窗口版本（相同 ETA、截止时刻、时区
                # 与 tzdata 版本）。本次实到货物计入新批次，已签收部分不会被误判为超时。
                self.connection.execute(
                    "UPDATE delivery_windows SET state='closed_carried',closes_at=? "
                    "WHERE window_id=? AND state IN ('open','partial')",
                    (scanned_text, judged_window_id),
                )
                carried_resolved = {
                    "facility_id": active["facility_id"],
                    "timezone": active["timezone"],
                    "tz_version": active["tz_version"],
                    "eta_local": active["eta_local"],
                    "eta_kind": active["eta_kind"],
                    "eta_at": active["eta_at"],
                    "deadline_at": active["deadline_at"],
                    "grace_hours": int(active["grace_hours"]),
                }
                judged_window_id = self._insert_window_row(
                    transfer_id,
                    judged_batch_no + 1,
                    carried_resolved,
                    remaining,
                    Decimal("0"),
                    "open",
                    actor_id,
                    carried_from_window_id=int(active["window_id"]),
                )
                judged_batch_no += 1
                self._audit("delivery_window", str(judged_window_id), "delivery.window_carried", actor_id, {
                    "transfer_id": transfer_id,
                    "batch_no": judged_batch_no,
                    "carried_from_window_id": int(active["window_id"]),
                    "carried_mwh": decimal_text(remaining),
                    "tz_version": active["tz_version"],
                })

            lot_id = self._post_destination_inventory(
                transfer=transfer,
                origin=origin,
                batch_no=judged_batch_no,
                quantity=quantity,
                received_at=scanned_text,
                actor_id=actor_id,
            )
            judged_row = self.connection.execute(
                "SELECT * FROM delivery_windows WHERE window_id=?", (judged_window_id,)
            ).fetchone()
            new_received = quantize_volume(Decimal(judged_row["received_mwh"]) + quantity)
            closes = new_received >= Decimal(judged_row["expected_mwh"])
            if closes:
                new_state = "closed_late" if is_late else "closed_on_time"
            else:
                new_state = "partial"
            cursor = self.connection.execute(
                "INSERT INTO delivery_scans(window_id,transfer_id,scanned_at,quantity_mwh,applied_mwh,"
                "classification,note,idempotency_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    judged_window_id,
                    transfer_id,
                    scanned_text,
                    quantity_text,
                    quantity_text,
                    "late" if is_late else "on_time",
                    note,
                    idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            scan_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE delivery_windows SET received_mwh=?,state=?,closes_at=? WHERE window_id=?",
                (decimal_text(new_received), new_state, scanned_text if closes else None, judged_window_id),
            )
            finalized = False
            if closes:
                finalized = self._finalize_delivery(transfer_id, scanned_at)
            response = {
                "scan_id": scan_id,
                "transfer_id": transfer_id,
                "batch_no": judged_batch_no,
                "scanned_at": scanned_text,
                "quantity_mwh": quantity_text,
                "applied_mwh": quantity_text,
                "classification": "late" if is_late else "on_time",
                "window_state": new_state,
                "destination_lot_id": lot_id,
            }
            if finalized:
                response["delivery_state"] = "delivered"
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('delivery_scan',?,?,?,?)",
                (idempotency_key, digest(raw), canonical_json(response), self._now()),
            )
            self._audit(
                "delivery_scan",
                str(scan_id),
                "delivery.scan_recorded",
                actor_id,
                {"transfer_id": transfer_id, "classification": response["classification"], "applied_mwh": quantity_text},
            )
        return response

    def _finalize_delivery(self, transfer_id: str, arrived_at: str) -> bool:
        """在当前事务内把送电单与提名标记为已签收，返回是否全部批次结清。"""
        open_count = self.connection.execute(
            "SELECT COUNT(*) AS c FROM delivery_windows WHERE transfer_id=? AND state IN ('open','partial')",
            (transfer_id,),
        ).fetchone()["c"]
        if open_count:
            return False
        total_rows = self.connection.execute(
            "SELECT received_mwh FROM delivery_windows WHERE transfer_id=?", (transfer_id,)
        ).fetchall()
        total = quantize_volume(sum((Decimal(row["received_mwh"]) for row in total_rows), Decimal("0")))
        nomination_id = self.connection.execute(
            "SELECT nomination_id FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()["nomination_id"]
        self.connection.execute(
            "UPDATE nominations SET delivered_mwh=?,state='delivered',revision=revision+1 WHERE nomination_id=?",
            (decimal_text(total), nomination_id),
        )
        self.connection.execute(
            "UPDATE transfers SET arrived_at=?,state='delivered',revision=revision+1 WHERE transfer_id=?",
            (arrived_at, transfer_id),
        )
        return True

    def _record_duplicate_scan(
        self,
        actor_id: str,
        transfer_id: str,
        scanned_at: datetime,
        quantity: Decimal,
        idempotency_key: str,
        original_scan_id: int,
        note: str,
    ) -> dict[str, Any]:
        original = self.connection.execute(
            "SELECT * FROM delivery_scans WHERE scan_id=?", (original_scan_id,)
        ).fetchone()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO delivery_scans(window_id,transfer_id,scanned_at,quantity_mwh,applied_mwh,"
                "classification,duplicates_scan_id,note,idempotency_key,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    original["window_id"],
                    transfer_id,
                    utc_text(scanned_at),
                    decimal_text(quantize_volume(quantity)),
                    "0.000",
                    "duplicate",
                    original_scan_id,
                    note,
                    idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            scan_id = int(cursor.lastrowid)
            response = {
                "scan_id": scan_id,
                "transfer_id": transfer_id,
                "batch_no": int(self.connection.execute(
                    "SELECT batch_no FROM delivery_windows WHERE window_id=?", (original["window_id"],)
                ).fetchone()["batch_no"]),
                "scanned_at": utc_text(scanned_at),
                "quantity_mwh": decimal_text(quantize_volume(quantity)),
                "applied_mwh": "0.000",
                "classification": "duplicate",
                "duplicates_scan_id": original_scan_id,
                "window_state": self.connection.execute(
                    "SELECT state FROM delivery_windows WHERE window_id=?", (original["window_id"],)
                ).fetchone()["state"],
            }
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('delivery_scan',?,?,?,?)",
                (idempotency_key, digest({"duplicate_of": original_scan_id}), canonical_json(response), self._now()),
            )
            self._audit(
                "delivery_scan",
                str(scan_id),
                "delivery.scan_duplicate",
                actor_id,
                {"transfer_id": transfer_id, "duplicates_scan_id": original_scan_id},
            )
        return response

    def delivery_status(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "delivery.read")
        transfer = self._transfer_destination(transfer_id)
        windows = self.connection.execute(
            "SELECT * FROM delivery_windows WHERE transfer_id=? ORDER BY batch_no", (transfer_id,)
        ).fetchall()
        if not windows:
            raise NotFound("到厂窗口不存在")
        scans = self.connection.execute(
            "SELECT * FROM delivery_scans WHERE transfer_id=? ORDER BY scan_id", (transfer_id,)
        ).fetchall()
        window_rows = [self._window_dict(row["window_id"]) for row in windows]
        # 只有首个批次承载原始待收；后续批次为结转，汇总时按各批次实收统计，避免重复计算待收。
        received_total = quantize_volume(sum((Decimal(row["received_mwh"]) for row in windows), Decimal("0")))
        nomination = self.connection.execute(
            "SELECT state,delivered_mwh FROM nominations WHERE nomination_id=?",
            (transfer["nomination_id"],),
        ).fetchone()
        lot_rows = self.connection.execute(
            "SELECT lot_id,quantity_mwh,available_mwh FROM inventory_lots "
            "WHERE lot_id LIKE ? ORDER BY lot_id",
            (f"{transfer_id}:b%",),
        ).fetchall()
        return {
            "transfer_id": transfer_id,
            "nomination_id": transfer["nomination_id"],
            "state": nomination["state"],
            "delivered_mwh": nomination["delivered_mwh"],
            "expected_original_mwh": windows[0]["expected_mwh"],
            "received_total_mwh": decimal_text(received_total),
            "windows": window_rows,
            "scans": [dict(row) for row in scans],
            "destination_inventory": [dict(row) for row in lot_rows],
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用电价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
