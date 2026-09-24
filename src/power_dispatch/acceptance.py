"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = SupplyService(connection, clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    # 夏令时切换周的燃料船：纽约本地 2026-11-01 01:30 重复出现，窗口冻结第一次出现（EDT）。
    dst_shipment = service.register_shipment("dispatch", {"shipment_id": "ship-dst", "facility_id": "ny-port", "product": "crude", "grade": "WTI", "expected_mwh": "100000", "unit_cost_cny": "91.25", "window_date": "2026-11-01", "window_start_local": "00:30", "window_end_local": "01:30"})
    first_receipt = service.record_shipment_receipt("dispatch", "ship-dst", {"receipt_id": "r-1", "arrived_at": "2026-11-01T05:00:00Z", "quantity_mwh": "60000"})
    second_receipt = service.record_shipment_receipt("dispatch", "ship-dst", {"receipt_id": "r-2", "arrived_at": "2026-11-01T05:20:00Z", "quantity_mwh": "40000"})
    service.register_shipment("dispatch", {"shipment_id": "ship-pending", "facility_id": "ny-port", "product": "crude", "grade": "WTI", "expected_mwh": "50000", "unit_cost_cny": "91.25", "window_date": "2026-11-01", "window_start_local": "00:30", "window_end_local": "01:30"})
    clock.current = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    late_scan = service.scan_shipments("dispatch")
    repeat_scan = service.scan_shipments("dispatch")
    ny_inventory = service.inventory_summary("ny-port", "crude")
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "arrivals": {"frozen_window_ends_at": dst_shipment["window_ends_at"], "calendar_version": dst_shipment["calendar_version"], "received_state": second_receipt["state"], "received_mwh": second_receipt["received_mwh"], "newly_overdue": late_scan["newly_overdue"], "repeat_scan_newly_overdue": repeat_scan["newly_overdue"], "ny_inventory_mwh": ny_inventory["available_mwh"]}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
