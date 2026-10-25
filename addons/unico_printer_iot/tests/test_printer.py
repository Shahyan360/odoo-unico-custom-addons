import json

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPrinterTray(TransactionCase):
    """Simple unit tests for SNMP tray monitoring on iot.device.

    These exercise the pure data path (token lifecycle, _apply_tray_data, tray
    status compute) and do NOT touch the network, SNMP, or any real printer.
    """

    def setUp(self):
        super().setUp()
        self.box = self.env["iot.box"].create({"name": "Test Box"})
        self.Device = self.env["iot.device"]

    def _printer(self, **vals):
        vals.setdefault("name", "Test Printer")
        vals.setdefault("iot_id", self.box.id)
        vals.setdefault("type", "printer")
        vals.setdefault("is_tray_printer", True)
        return self.Device.create(vals)

    def test_push_token_generated_on_create(self):
        device = self._printer()
        self.assertTrue(device.push_token, "Tray printer should get a token on create")

    def test_push_token_generated_when_flagged_later(self):
        device = self.Device.create({
            "name": "P", "iot_id": self.box.id, "type": "printer",
        })
        self.assertFalse(device.push_token)
        device.is_tray_printer = True
        self.assertTrue(device.push_token, "Flagging as tray printer should mint a token")

    def test_regenerate_token_changes_it(self):
        device = self._printer()
        old = device.push_token
        device.action_regenerate_token()
        self.assertTrue(device.push_token)
        self.assertNotEqual(device.push_token, old)

    def test_apply_tray_data_creates_trays(self):
        device = self._printer()
        device._apply_tray_data([
            {"index": "1.1", "name": "Cassette 1", "current": 0, "max": 500},
            {"index": "1.2", "name": "Cassette 2", "current": 500, "max": 500},
        ])
        self.assertEqual(device.tray_count, 2)

    def test_apply_tray_data_updates_and_removes(self):
        device = self._printer()
        device._apply_tray_data([
            {"index": "1.1", "name": "Cassette 1", "current": 500, "max": 500},
            {"index": "1.2", "name": "Cassette 2", "current": 500, "max": 500},
        ])
        device._apply_tray_data([
            {"index": "1.1", "name": "Cassette 1", "current": 0, "max": 500},
            {"index": "1.3", "name": "Cassette 3", "current": 250, "max": 500},
        ])
        indexes = set(device.tray_ids.mapped("tray_index"))
        self.assertEqual(indexes, {"1.1", "1.3"})
        self.assertEqual(device.tray_ids.filtered(lambda t: t.tray_index == "1.1").current_level, 0)

    def test_apply_tray_data_rejects_oversized(self):
        device = self._printer()
        big = [{"index": str(i), "name": "t", "current": 1, "max": 2} for i in range(65)]
        with self.assertRaises(UserError):
            device._apply_tray_data(big)

    def test_tray_status_and_percent(self):
        device = self._printer()
        device._apply_tray_data([
            {"index": "1", "name": "ok", "current": 400, "max": 500},
            {"index": "2", "name": "low", "current": 50, "max": 500},
            {"index": "3", "name": "empty", "current": 0, "max": 500},
            {"index": "4", "name": "unknown", "current": -2, "max": -2},
            {"index": "5", "name": "full_unmeasured", "current": -3, "max": 500},
        ])
        by_name = {t.name: t for t in device.tray_ids}
        self.assertEqual(by_name["ok"].status, "ok")
        self.assertEqual(by_name["low"].status, "low")
        self.assertEqual(by_name["empty"].status, "empty")
        self.assertEqual(by_name["unknown"].status, "unknown")
        self.assertEqual(by_name["full_unmeasured"].status, "ok")
        self.assertAlmostEqual(by_name["ok"].level_percent, 80.0)

    def test_low_threshold_is_respected(self):
        device = self._printer()
        device._apply_tray_data([{"index": "1", "name": "t", "current": 120, "max": 500}])
        tray = device.tray_ids
        self.assertEqual(tray.status, "ok")  # 24% > default 20%
        tray.low_threshold = 25
        self.assertEqual(tray.status, "low")  # 24% <= 25%

    def test_low_tray_count_rollup(self):
        device = self._printer()
        device._apply_tray_data([
            {"index": "1", "name": "a", "current": 0, "max": 500},     # empty
            {"index": "2", "name": "b", "current": 10, "max": 500},    # low
            {"index": "3", "name": "c", "current": 500, "max": 500},   # ok
        ])
        self.assertEqual(device.low_tray_count, 2)

    def test_agent_config_json_is_pastable(self):
        device = self._printer(tray_snmp_ip="10.0.0.9", snmp_community="public")
        self.assertIn("/printer_iot/push", device.push_url)
        cfg = json.loads(device.agent_config_json)
        self.assertEqual(cfg["printer_ip"], "10.0.0.9")
        self.assertEqual(cfg["push_token"], device.push_token)
        self.assertEqual(cfg["community"], "public")
