from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from power_dispatch.service import SupplyService
from power_dispatch.storage import connect


def build_service(connection: sqlite3.Connection, clock=None) -> SupplyService:
    service = SupplyService(connection, clock or FrozenClock(datetime(2026, 3, 1, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("disp", "dispatcher"), ("risk", "risk"), ("aud", "auditor")):
        service.create_user(user_id, user_id, role)
    service.create_facility("plan", {"facility_id": "plant", "name": "沿海电厂", "kind": "storage", "timezone": "America/New_York", "capacity_mwh": "999"})
    service.create_facility("plan", {"facility_id": "term", "name": "始发终端", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "999"})
    service.create_route("plan", {"route_id": "r1", "origin_id": "term", "destination_id": "plant", "product": "crude", "daily_capacity": "1000", "loss_basis_points": 0, "transit_hours": 1})
    service.add_inventory_lot("disp", {"lot_id": "l1", "facility_id": "term", "product": "crude", "grade": "WTI", "quantity_mwh": "500", "unit_cost_cny": "90", "received_at": "2026-02-28T00:00:00Z"})
    return service


def dispatch(service: SupplyService, transfer_id: str, nomination_id: str, service_date: str, requested: str, key: str) -> None:
    service.submit_nomination("disp", {"nomination_id": nomination_id, "route_id": "r1", "shipper_id": "ship", "service_date": service_date, "requested_mwh": requested, "priority": 10, "idempotency_key": key})
    service.allocate("disp", "r1", service_date)
    service.dispatch_transfer("disp", transfer_id, nomination_id, "l1", 2)


class DeliveryWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = build_service(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_repeated_fallback_time_uses_second_occurrence(self) -> None:
        # 纽约 2026-11-01 01:30 出现两次：05:30Z(EDT) 与 06:30Z(EST)，取第二次。
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        window = self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        self.assertEqual(window["eta_kind"], "repeated")
        self.assertEqual(window["eta_at"], "2026-11-01T06:30:00Z")
        self.assertEqual(window["deadline_at"], "2026-11-01T06:30:00Z")
        # 06:00Z 处在重复时刻的第一次出现区间，旧逻辑可能误算成“下一天”，这里必须判准时。
        scan = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s1"})
        self.assertEqual(scan["classification"], "on_time")
        self.assertEqual(scan["window_state"], "closed_on_time")

    def test_missing_spring_forward_time_is_forward_resolved(self) -> None:
        # 纽约 2026-03-08 02:30 不存在（春令时跳过），顺延到 03:30 EDT = 07:30Z。
        dispatch(self.service, "t1", "n1", "2026-03-08", "50", "k1")
        window = self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-03-08T02:30:00", "grace_hours": 0})
        self.assertEqual(window["eta_kind"], "gap")
        self.assertEqual(window["eta_at"], "2026-03-08T07:30:00Z")
        # 07:00Z = 本地 03:00 EDT，跳变之后、顺延 ETA 之前，仍判准时。
        scan = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-03-08T07:00:00Z", "quantity_mwh": "50", "idempotency_key": "s1"})
        self.assertEqual(scan["classification"], "on_time")

    def test_window_crosses_midnight_in_local_time(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-03", "100", "k1")
        window = self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-03T23:30:00", "grace_hours": 2})
        # 23:30 EST = 次日 04:30Z；宽限两小时后跨本地午夜 = 06:30Z。
        self.assertEqual(window["eta_at"], "2026-11-04T04:30:00Z")
        self.assertEqual(window["deadline_at"], "2026-11-04T06:30:00Z")
        on_time = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-04T06:00:00Z", "quantity_mwh": "60", "idempotency_key": "s1"})
        self.assertEqual(on_time["classification"], "on_time")
        late = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-04T07:00:00Z", "quantity_mwh": "40", "idempotency_key": "s2"})
        self.assertEqual(late["classification"], "late")

    def test_late_scan_carries_only_remaining_quantity_and_preserves_on_time_part(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        first = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "60", "idempotency_key": "s1"})
        self.assertEqual(first["window_state"], "partial")
        late = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T07:00:00Z", "quantity_mwh": "40", "idempotency_key": "s2"})
        self.assertEqual(late["classification"], "late")
        self.assertEqual(late["batch_no"], 2)

        status = self.service.delivery_status("aud", "t1")
        windows = [(w["batch_no"], w["state"], w["expected_mwh"], w["received_mwh"]) for w in status["windows"]]
        # 准时的 60 保留在批次 1 且不被标记超时；只有新增的 40 结转到批次 2。
        self.assertEqual(windows, [(1, "closed_carried", "100.000", "60.000"), (2, "closed_late", "40.000", "40.000")])
        self.assertEqual(status["state"], "delivered")
        self.assertEqual(status["delivered_mwh"], "100.000")
        self.assertEqual(status["received_total_mwh"], "100.000")
        inventory = {row["lot_id"]: row["quantity_mwh"] for row in status["destination_inventory"]}
        self.assertEqual(inventory, {"t1:b1": "60.000", "t1:b2": "40.000"})

    def test_late_quantity_larger_than_remaining_is_rejected(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "60", "idempotency_key": "s1"})
        with self.assertRaises(Conflict):
            self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T07:00:00Z", "quantity_mwh": "41", "idempotency_key": "s2"})

    def test_duplicate_scans_do_not_change_inventory_or_window(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        original = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s1"})
        # 同一幂等键重放返回完全相同的结果。
        replay = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s1"})
        self.assertEqual(replay, original)
        # 不同幂等键但同一时刻、同一数量的自然重复，记为 duplicate 且不结转库存。
        duplicate = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s1-dup"})
        self.assertEqual(duplicate["classification"], "duplicate")
        self.assertEqual(duplicate["applied_mwh"], "0.000")
        self.assertEqual(duplicate["duplicates_scan_id"], original["scan_id"])
        status = self.service.delivery_status("aud", "t1")
        self.assertEqual(status["received_total_mwh"], "100.000")
        self.assertEqual([row["quantity_mwh"] for row in status["destination_inventory"]], ["100.000"])
        classifications = [scan["classification"] for scan in status["scans"]]
        self.assertEqual(classifications, ["on_time", "duplicate"])

    def test_scan_after_full_close_is_rejected_but_duplicate_still_recorded(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s1"})
        with self.assertRaises(InvalidState):
            self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-05T00:00:00Z", "quantity_mwh": "5", "idempotency_key": "s2"})
        duplicate = self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "100", "idempotency_key": "s3"})
        self.assertEqual(duplicate["classification"], "duplicate")

    def test_inputs_must_carry_timezone_or_be_facility_local(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        # 签收扫描必须带时区偏移。
        with self.assertRaises(ValidationFailed):
            self.service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00", "quantity_mwh": "1", "idempotency_key": "x"})
        # ETA 必须是设施本地日历时间，携带偏移要拒绝。
        with self.assertRaises(ValidationFailed):
            self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00-05:00", "grace_hours": 0, "batch_no": 2})
        with self.assertRaises(ValidationFailed):
            self.service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01", "grace_hours": 0, "batch_no": 3})

    def test_role_required_for_delivery_endpoints(self) -> None:
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")
        with self.assertRaises(Forbidden):
            self.service.open_delivery_window("aud", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        with self.assertRaises(Forbidden):
            self.service.record_delivery_scan("plan", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "1", "idempotency_key": "x"})


class DeliveryRestartTests(unittest.TestCase):
    def test_frozen_window_version_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dispatch.sqlite3"
            connection = connect(database)
            service = build_service(connection, FrozenClock(datetime(2026, 3, 1, tzinfo=timezone.utc)))
            dispatch(service, "t1", "n1", "2026-11-01", "100", "k1")
            window = service.open_delivery_window("disp", "t1", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
            frozen_version = window["tz_version"]
            service.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "60", "idempotency_key": "s1"})
            connection.close()

            # 新进程、新服务实例（甚至使用真实系统时钟）重新打开同一数据库。
            restarted = connect(database)
            service2 = SupplyService(restarted)
            status = service2.delivery_status("aud", "t1")
            window_row = status["windows"][0]
            self.assertEqual(window_row["eta_at"], "2026-11-01T06:30:00Z")
            self.assertEqual(window_row["tz_version"], frozen_version)
            self.assertEqual(window_row["received_mwh"], "60.000")
            late = service2.record_delivery_scan("disp", "t1", {"scanned_at": "2026-11-01T07:00:00Z", "quantity_mwh": "40", "idempotency_key": "s2"})
            self.assertEqual(late["classification"], "late")
            self.assertEqual(late["batch_no"], 2)
            carried = service2.delivery_status("aud", "t1")["windows"][1]
            self.assertEqual(carried["tz_version"], frozen_version)
            self.assertEqual(carried["deadline_at"], "2026-11-01T06:30:00Z")
            self.assertTrue(service2.audit_chain("aud")["valid"])
            restarted.close()


class DeliveryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = build_service(self.connection)
        self.app = JsonApplication(self.service)
        dispatch(self.service, "t1", "n1", "2026-11-01", "100", "k1")

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, actor: str, payload: dict) -> object:
        import json
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def test_delivery_flow_visible_through_http_api(self) -> None:
        opened = self._post("/transfers/t1/delivery-window", "disp", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        self.assertEqual(opened.status, 201)
        self.assertEqual(opened.body["eta_kind"], "repeated")

        first = self._post("/transfers/t1/delivery-scans", "disp", {"scanned_at": "2026-11-01T06:00:00Z", "quantity_mwh": "60", "idempotency_key": "s1"})
        self.assertEqual(first.status, 201)
        self.assertEqual(first.body["classification"], "on_time")

        late = self._post("/transfers/t1/delivery-scans", "disp", {"scanned_at": "2026-11-01T07:00:00Z", "quantity_mwh": "40", "idempotency_key": "s2"})
        self.assertEqual(late.body["classification"], "late")
        self.assertEqual(late.body["batch_no"], 2)

        status = self.app.handle("GET", "/transfers/t1/delivery", {"X-Actor-Id": "aud"})
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["received_total_mwh"], "100.000")
        self.assertEqual([w["state"] for w in status.body["windows"]], ["closed_carried", "closed_late"])
        self.assertEqual(
            {row["lot_id"]: row["available_mwh"] for row in status.body["destination_inventory"]},
            {"t1:b1": "60.000", "t1:b2": "40.000"},
        )
        self.assertEqual([s["classification"] for s in status.body["scans"]], ["on_time", "late"])

    def test_naive_scan_rejected_via_api(self) -> None:
        self._post("/transfers/t1/delivery-window", "disp", {"eta_local": "2026-11-01T01:30:00", "grace_hours": 0})
        response = self._post("/transfers/t1/delivery-scans", "disp", {"scanned_at": "2026-11-01T06:00:00", "quantity_mwh": "1", "idempotency_key": "x"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
