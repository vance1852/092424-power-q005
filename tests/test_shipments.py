from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, ValidationFailed
from power_dispatch.service import SupplyService
from power_dispatch.storage import connect


def frozen_service(connection, when: str) -> tuple[SupplyService, FrozenClock]:
    clock = FrozenClock(datetime.fromisoformat(when.replace("Z", "+00:00")))
    service = SupplyService(connection, clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    return service, clock


class ArrivalWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service, self.clock = frozen_service(
            self.connection, "2026-10-31T20:00:00Z"
        )
        # 纽约沿海设施：2026-11-01 02:00 本地发生秋季回拨，01:30 重复两次。
        self.service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})
        self.service.create_facility("plan", {"facility_id": "sh-port", "name": "上海港", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})

    def tearDown(self) -> None:
        self.connection.close()

    def register(self, shipment_id: str = "ship-dst", **overrides) -> dict:
        payload = {
            "shipment_id": shipment_id,
            "facility_id": "ny-port",
            "product": "crude",
            "grade": "WTI",
            "expected_mwh": "100",
            "unit_cost_cny": "91.5",
            "window_date": "2026-11-01",
            "window_start_local": "00:30",
            "window_end_local": "01:30",
        }
        payload.update(overrides)
        return self.service.register_shipment("dispatch", payload)

    def test_repeated_wall_time_uses_first_occurrence_not_next_day(self) -> None:
        shipment = self.register()
        # 本地 01:30 重复出现：第一次（EDT, -04:00）= 05:30Z，第二次（EST, -05:00）= 06:30Z。
        self.assertEqual(shipment["window_starts_at"], "2026-11-01T04:30:00Z")
        self.assertEqual(shipment["window_ends_at"], "2026-11-01T05:30:00Z")
        self.assertEqual(shipment["end_resolution"], "ambiguous_first")
        self.assertTrue(shipment["calendar_version"])
        first = self.service.record_shipment_receipt("dispatch", "ship-dst", {
            "receipt_id": "r-1", "arrived_at": "2026-11-01T01:30:00-04:00", "quantity_mwh": "100",
        })
        self.assertEqual(first["state"], "received")
        self.assertTrue(first["on_time"])
        self.assertFalse(first["last_receipt"]["late"])

    def test_second_occurrence_of_repeated_time_is_late(self) -> None:
        self.register()
        second_occurrence = self.service.record_shipment_receipt("dispatch", "ship-dst", {
            "receipt_id": "r-1", "arrived_at": "2026-11-01T01:30:00-05:00", "quantity_mwh": "100",
        })
        # 旧逻辑把重复时刻误判为下一天；现在按冻结窗口精确判定为超时但仍在当日。
        self.assertTrue(second_occurrence["last_receipt"]["late"])
        self.assertFalse(second_occurrence["on_time"])
        self.assertEqual(second_occurrence["state"], "received")

    def test_spring_forward_gap_shifts_window_start_forward(self) -> None:
        # 2026-03-08 02:30 纽约本地不存在（02:00->03:00 跳时），前移到 03:30 = 07:30Z。
        shipment = self.register(
            "ship-gap", window_date="2026-03-08",
            window_start_local="02:30", window_end_local="04:30",
        )
        self.assertEqual(shipment["window_starts_at"], "2026-03-08T07:30:00Z")
        self.assertEqual(shipment["window_ends_at"], "2026-03-08T08:30:00Z")
        self.assertEqual(shipment["start_resolution"], "gap_shifted")
        with self.assertRaises(Conflict):
            self.service.record_shipment_receipt("dispatch", "ship-gap", {
                "receipt_id": "r-early", "arrived_at": "2026-03-08T07:00:00Z", "quantity_mwh": "10",
            })

    def test_cross_midnight_window(self) -> None:
        shipment = self.register(
            "ship-night", facility_id="sh-port", window_date="2026-09-24",
            window_start_local="22:00", window_end_local="02:00",
        )
        self.assertTrue(shipment["crosses_midnight"])
        self.assertEqual(shipment["window_starts_at"], "2026-09-24T14:00:00Z")
        self.assertEqual(shipment["window_ends_at"], "2026-09-24T18:00:00Z")
        within = self.service.record_shipment_receipt("dispatch", "ship-night", {
            "receipt_id": "r-1", "arrived_at": "2026-09-24T17:30:00Z", "quantity_mwh": "100",
        })
        self.assertFalse(within["last_receipt"]["late"])
        with self.assertRaises(Conflict):
            self.service.record_shipment_receipt("dispatch", "ship-night", {
                "receipt_id": "r-late", "arrived_at": "2026-09-24T13:00:00Z", "quantity_mwh": "10",
            })

    def test_end_of_day_local_means_following_midnight(self) -> None:
        shipment = self.register(
            "ship-eod", facility_id="sh-port", window_date="2026-09-24",
            window_start_local="08:00", window_end_local="24:00",
        )
        self.assertEqual(shipment["window_ends_at"], "2026-09-24T16:00:00Z")
        self.assertTrue(shipment["crosses_midnight"])

    def test_arrived_at_must_carry_timezone(self) -> None:
        self.register()
        with self.assertRaises(ValidationFailed):
            self.service.record_shipment_receipt("dispatch", "ship-dst", {
                "receipt_id": "r-bad", "arrived_at": "2026-11-01 05:00:00", "quantity_mwh": "10",
            })

    def test_facility_timezone_must_be_iana(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_facility("plan", {"facility_id": "bad-tz", "name": "错时区", "kind": "terminal", "timezone": "EST", "capacity_mwh": "10"})


class PartialReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service, self.clock = frozen_service(
            self.connection, "2026-10-31T20:00:00Z"
        )
        self.service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})
        self.service.register_shipment("dispatch", {
            "shipment_id": "ship-1", "facility_id": "ny-port", "product": "crude",
            "grade": "WTI", "expected_mwh": "100", "unit_cost_cny": "90",
            "window_date": "2026-11-01", "window_start_local": "00:00", "window_end_local": "04:00",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def receipt(self, receipt_id: str, quantity: str, at: str = "2026-11-01T05:00:00Z", **extra) -> dict:
        payload = {"receipt_id": receipt_id, "arrived_at": at, "quantity_mwh": quantity}
        payload.update(extra)
        return self.service.record_shipment_receipt("dispatch", "ship-1", payload)

    def test_partial_receipts_carry_forward_only_new_quantity(self) -> None:
        first = self.receipt("r-1", "60")
        self.assertEqual(first["state"], "partial")
        self.assertEqual(first["received_mwh"], "60.000")
        self.assertEqual(first["last_receipt"]["delta_mwh"], "60.000")
        second = self.receipt("r-2", "40", at="2026-11-01T06:00:00Z")
        self.assertEqual(second["state"], "received")
        # 第二次只结转新增的 40，累计 100，库存不重复计算第一次的 60。
        self.assertEqual(second["received_mwh"], "100.000")
        self.assertEqual(second["last_receipt"]["delta_mwh"], "40.000")
        lots = self.connection.execute(
            "SELECT lot_id,quantity_mwh FROM inventory_lots WHERE facility_id='ny-port' ORDER BY lot_id"
        ).fetchall()
        self.assertEqual([(row["lot_id"], row["quantity_mwh"]) for row in lots], [
            ("ship-1-r-1", "60.000"), ("ship-1-r-2", "40.000"),
        ])
        summary = self.service.inventory_summary("ny-port", "crude")
        self.assertEqual(summary["available_mwh"], "100.000")

    def test_overrun_requires_explicit_acceptance(self) -> None:
        self.receipt("r-1", "60")
        with self.assertRaises(Conflict):
            self.receipt("r-2", "50")
        accepted = self.receipt("r-2", "50", accept_overrun=True)
        self.assertEqual(accepted["received_mwh"], "110.000")
        self.assertTrue(accepted["overrun_accepted"])
        self.assertEqual(self.service.inventory_summary("ny-port", "crude")["available_mwh"], "110.000")

    def test_duplicate_receipt_id_is_rejected(self) -> None:
        self.receipt("r-1", "60")
        with self.assertRaises(Conflict):
            self.receipt("r-1", "60")
        self.assertEqual(self.service.inventory_summary("ny-port", "crude")["available_mwh"], "60.000")


class ShipmentScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service, self.clock = frozen_service(
            self.connection, "2026-11-01T05:31:00Z"
        )
        self.service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})
        for shipment_id, expected in (("ship-expected", "100"), ("ship-partial", "100"), ("ship-done", "100")):
            self.service.register_shipment("dispatch", {
                "shipment_id": shipment_id, "facility_id": "ny-port", "product": "crude",
                "grade": "WTI", "expected_mwh": expected, "unit_cost_cny": "90",
                "window_date": "2026-11-01", "window_start_local": "00:30", "window_end_local": "01:30",
            })
        # 窗口内完成一艘；部分签收一艘。
        self.service.record_shipment_receipt("dispatch", "ship-done", {
            "receipt_id": "d-1", "arrived_at": "2026-11-01T05:00:00Z", "quantity_mwh": "100",
        })
        self.service.record_shipment_receipt("dispatch", "ship-partial", {
            "receipt_id": "p-1", "arrived_at": "2026-11-01T05:00:00Z", "quantity_mwh": "40",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def test_late_scan_and_repeat_scan_leave_inventory_intact(self) -> None:
        before = self.service.inventory_summary("ny-port", "crude")["available_mwh"]
        scan = self.service.scan_shipments("dispatch")
        self.assertFalse(scan["repeat_scan"])
        self.assertEqual(sorted(scan["newly_overdue"]), ["ship-expected", "ship-partial"])
        self.assertEqual(self.service.shipment("ship-expected")["state"], "overdue")
        self.assertEqual(self.service.shipment("ship-partial")["state"], "overdue")
        self.assertEqual(self.service.shipment("ship-done")["state"], "received")
        # 已签收的货量不能被标记成超时冲销；库存不变。
        self.assertEqual(self.service.inventory_summary("ny-port", "crude")["available_mwh"], before)

        repeated = self.service.scan_shipments("dispatch")
        self.assertTrue(repeated["repeat_scan"])
        self.assertEqual(repeated["newly_overdue"], [])
        self.assertEqual(len(repeated["overdue"]), 2)
        self.assertEqual(self.service.inventory_summary("ny-port", "crude")["available_mwh"], before)

    def test_remainder_arriving_after_overdue_completes_and_keeps_late_flag(self) -> None:
        self.service.scan_shipments("dispatch")
        late = self.service.record_shipment_receipt("dispatch", "ship-partial", {
            "receipt_id": "p-2", "arrived_at": "2026-11-01T08:00:00Z", "quantity_mwh": "60",
        })
        self.assertTrue(late["last_receipt"]["late"])
        self.assertEqual(late["state"], "received")
        self.assertEqual(late["received_mwh"], "100.000")
        self.assertEqual(self.service.inventory_summary("ny-port", "crude")["available_mwh"], "200.000")

    def test_scan_before_window_end_marks_nothing(self) -> None:
        early_service, _ = frozen_service(
            sqlite3.connect(":memory:", isolation_level=None), "2026-11-01T04:00:00Z"
        )
        early_service.connection.row_factory = sqlite3.Row
        early_service.create_facility("plan", {"facility_id": "port-f", "name": "港", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "10"})
        early_service.register_shipment("dispatch", {
            "shipment_id": "ship-x", "facility_id": "port-f", "product": "crude", "grade": "WTI",
            "expected_mwh": "10", "unit_cost_cny": "90",
            "window_date": "2026-11-01", "window_start_local": "12:00", "window_end_local": "14:00",
        })
        scan = early_service.scan_shipments("dispatch")
        self.assertEqual(scan["newly_overdue"], [])
        self.assertTrue(scan["repeat_scan"])
        early_service.connection.close()


class FrozenWindowPersistenceTests(unittest.TestCase):
    def test_window_version_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dispatch.sqlite3"
            connection = connect(database)
            service = SupplyService(connection, FrozenClock(datetime(2026, 10, 31, 20, tzinfo=timezone.utc)))
            for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher")):
                service.create_user(user_id, user_id, role)
            service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})
            registered = service.register_shipment("dispatch", {
                "shipment_id": "ship-restart", "facility_id": "ny-port", "product": "crude",
                "grade": "WTI", "expected_mwh": "100", "unit_cost_cny": "90",
                "window_date": "2026-11-01", "window_start_local": "00:30", "window_end_local": "01:30",
            })
            connection.close()

            restarted_connection = connect(database)
            restarted = SupplyService(
                restarted_connection,
                FrozenClock(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)),
            )
            shipment = restarted.shipment("ship-restart")
            # 仍按发运时冻结的窗口版本判定，不随重启重新解析。
            self.assertEqual(shipment["window_ends_at"], registered["window_ends_at"])
            self.assertEqual(shipment["calendar_version"], registered["calendar_version"])
            self.assertEqual(shipment["gap_policy"], "shift_forward")
            self.assertEqual(shipment["ambiguity_policy"], "first_occurrence")
            scan = restarted.scan_shipments("dispatch")
            self.assertEqual(scan["newly_overdue"], ["ship-restart"])
            restarted_connection.close()


class ShipmentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service, _ = frozen_service(self.connection, "2026-11-01T06:00:00Z")
        self.app = JsonApplication(self.service)
        self.service.create_facility("plan", {"facility_id": "ny-port", "name": "纽约港", "kind": "terminal", "timezone": "America/New_York", "capacity_mwh": "800000"})

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str = "dispatch"):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": actor},
            json.dumps(payload).encode("utf-8"),
        )

    def test_cross_midnight_window_is_visible_via_http(self) -> None:
        self.service.create_facility("plan", {"facility_id": "sh-port", "name": "上海港", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
        register = self.post("/shipments", {
            "shipment_id": "ship-night", "facility_id": "sh-port", "product": "crude",
            "grade": "ESPO", "expected_mwh": "100", "unit_cost_cny": "90",
            "window_date": "2026-09-24", "window_start_local": "22:00", "window_end_local": "02:00",
        })
        self.assertEqual(register.status, 201)
        self.assertTrue(register.body["crosses_midnight"])
        self.assertEqual(register.body["window_starts_at"], "2026-09-24T14:00:00Z")
        self.assertEqual(register.body["window_ends_at"], "2026-09-24T18:00:00Z")
        receipt = self.post("/shipments/ship-night/receipts", {
            "receipt_id": "r-1", "arrived_at": "2026-09-24T17:30:00Z", "quantity_mwh": "100",
        })
        self.assertEqual(receipt.status, 201)
        self.assertFalse(receipt.body["last_receipt"]["late"])

    def test_register_receipt_scan_and_status_via_http(self) -> None:
        register = self.post("/shipments", {
            "shipment_id": "ship-api", "facility_id": "ny-port", "product": "crude",
            "grade": "WTI", "expected_mwh": "100", "unit_cost_cny": "90",
            "window_date": "2026-11-01", "window_start_local": "00:30", "window_end_local": "01:30",
        })
        self.assertEqual(register.status, 201)
        self.assertEqual(register.body["window_ends_at"], "2026-11-01T05:30:00Z")

        receipt = self.post("/shipments/ship-api/receipts", {
            "receipt_id": "r-1", "arrived_at": "2026-11-01T05:00:00Z", "quantity_mwh": "40",
        })
        self.assertEqual(receipt.status, 201)
        self.assertEqual(receipt.body["received_mwh"], "40.000")
        self.assertEqual(receipt.body["state"], "partial")

        naive = self.post("/shipments/ship-api/receipts", {
            "receipt_id": "r-bad", "arrived_at": "2026-11-01 05:00:00", "quantity_mwh": "10",
        })
        self.assertEqual(naive.status, 422)
        self.assertEqual(naive.body["error"]["code"], "validation_failed")

        scan = self.post("/shipments/scan", {})
        self.assertEqual(scan.status, 200)
        self.assertEqual(scan.body["newly_overdue"], ["ship-api"])

        status = self.app.handle("GET", "/shipments/ship-api", {"X-Actor-Id": "dispatch"})
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["state"], "overdue")
        self.assertEqual(len(status.body["receipts"]), 1)

        listing = self.app.handle("GET", "/shipments?facility_id=ny-port", {"X-Actor-Id": "dispatch"})
        self.assertEqual([item["shipment_id"] for item in listing.body["shipments"]], ["ship-api"])


if __name__ == "__main__":
    unittest.main()
