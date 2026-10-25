from odoo import api, fields, models

# Printer-MIB special negative values for prtInputCurrentLevel / MaxCapacity.
_SPECIAL = {-1: "other", -2: "unknown", -3: "full"}


class PrinterTray(models.Model):
    _name = "printer.tray"
    _description = "Printer Paper Tray"
    _order = "device_id, tray_index"

    device_id = fields.Many2one(
        "iot.device", required=True, ondelete="cascade", index=True,
        string="Printer",
    )
    tray_index = fields.Char(required=True, help="SNMP sub-index, e.g. 1.2.")
    name = fields.Char(string="Tray")
    current_level = fields.Integer(help="Sheets remaining. -2 = unknown, -3 = full (unmeasured).")
    max_capacity = fields.Integer(help="Tray capacity in sheets. -2 = unknown.")
    level_percent = fields.Float(
        string="Level %", compute="_compute_level", store=True, aggregator="avg"
    )
    status = fields.Selection(
        [
            ("empty", "Empty"),
            ("low", "Low"),
            ("ok", "OK"),
            ("unknown", "Unknown"),
        ],
        compute="_compute_level", store=True,
    )
    low_threshold = fields.Integer(
        string="Low % Threshold", default=20,
        help="At or below this percentage the tray is flagged 'Low'.",
    )
    last_update = fields.Datetime(readonly=True)

    @api.depends("current_level", "max_capacity", "low_threshold")
    def _compute_level(self):
        for tray in self:
            cur, mx = tray.current_level, tray.max_capacity
            if cur is None or cur < 0 or mx is None or mx <= 0:
                tray.level_percent = 0.0
                # -3 means "full but level not measurable" -> treat as OK
                tray.status = "ok" if cur == -3 else "unknown"
                continue
            pct = min(100.0, (cur / mx) * 100.0)
            tray.level_percent = pct
            if cur == 0:
                tray.status = "empty"
            elif pct <= (tray.low_threshold or 20):
                tray.status = "low"
            else:
                tray.status = "ok"

    @api.depends("device_id.name", "name", "tray_index")
    def _compute_display_name(self):
        for t in self:
            t.display_name = "%s / %s" % (t.device_id.name, t.name or t.tray_index)
