import json
import logging
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import snmp_client

_logger = logging.getLogger(__name__)

# Standard Printer-MIB (RFC 3805) prtInputTable column OIDs. Pre-filled so a
# typical Kyocera TASKalfa works without touching them.
DEFAULT_OID_NAME = "1.3.6.1.2.1.43.8.2.1.13"      # prtInputName
DEFAULT_OID_LEVEL = "1.3.6.1.2.1.43.8.2.1.10"     # prtInputCurrentLevel
DEFAULT_OID_MAX = "1.3.6.1.2.1.43.8.2.1.9"        # prtInputMaxCapacity

# Hard cap on how many trays a single push/poll may carry, so a malformed or
# malicious payload cannot create unbounded records.
MAX_TRAYS = 64


class IotDevice(models.Model):
    """Extend the native IoT device with optional SNMP paper-tray monitoring.

    Printing itself stays 100% native IoT (the standard 'Reports to Auto-Print'
    / report_ids routing). This adds, only for devices flagged as tray printers,
    a periodic read of paper-tray levels over SNMP.
    """
    _inherit = "iot.device"

    is_tray_printer = fields.Boolean(
        string="Tray Printer",
        help="Monitor this printer's paper-tray levels over SNMP.",
    )
    tray_connection_mode = fields.Selection(
        [
            ("agent", "Agent push (IoT box sends data)"),
            ("direct", "Direct poll (Odoo reaches the printer)"),
        ],
        string="Tray Data Source", default="agent",
        help="Agent: a script on the IoT box does the SNMP walk and POSTs here "
             "(use when Odoo cannot reach the printer LAN, e.g. cloud Odoo).\n"
             "Direct: the Odoo server SNMP-walks the printer on a schedule.",
    )

    # --- SNMP connection (printer's own IP, not the IoT box IP) ---
    tray_snmp_ip = fields.Char(string="Printer IP", help="e.g. 192.168.1.115.")
    snmp_port = fields.Integer(string="SNMP Port", default=161)
    snmp_version = fields.Selection(
        [("v1", "v1"), ("v2c", "v2c")], string="SNMP Version", default="v2c"
    )
    snmp_community = fields.Char(string="Read Community", default="public")
    oid_tray_name = fields.Char(string="OID: Tray Name", default=DEFAULT_OID_NAME)
    oid_current_level = fields.Char(string="OID: Current Level", default=DEFAULT_OID_LEVEL)
    oid_max_capacity = fields.Char(string="OID: Max Capacity", default=DEFAULT_OID_MAX)

    # --- Agent-push auth (secret: admin-only) ---
    push_token = fields.Char(
        string="Push Token", copy=False, groups="iot.group_iot_admin",
        help="Secret used by the IoT-box agent to authenticate POSTs.",
    )
    push_url = fields.Char(
        string="Push URL", compute="_compute_push_config", groups="iot.group_iot_admin",
    )
    agent_config_json = fields.Text(
        string="IoT Agent Config", compute="_compute_push_config",
        groups="iot.group_iot_admin",
        help="Copy/paste straight into agent_config.json on the Windows IoT box.",
    )

    # --- Tray state ---
    tray_ids = fields.One2many("printer.tray", "device_id", string="Trays")
    tray_count = fields.Integer(compute="_compute_tray_count")
    low_tray_count = fields.Integer(compute="_compute_tray_count")
    last_poll = fields.Datetime(string="Trays Updated", readonly=True)
    last_status = fields.Selection(
        [("never", "Never"), ("ok", "OK"), ("error", "Error")],
        string="Tray Status", default="never", readonly=True,
    )
    last_error = fields.Text(readonly=True)

    @api.depends("tray_ids.status")
    def _compute_tray_count(self):
        for device in self:
            device.tray_count = len(device.tray_ids)
            device.low_tray_count = len(
                device.tray_ids.filtered(lambda t: t.status in ("empty", "low"))
            )

    @api.depends("push_token", "tray_snmp_ip", "snmp_port", "snmp_version", "snmp_community")
    def _compute_push_config(self):
        base = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        for device in self:
            device.push_url = (base or "") + "/printer_iot/push"
            device.agent_config_json = json.dumps({
                "odoo_url": base or "https://YOUR-ODOO-URL",
                "push_token": device.push_token or "",
                "printer_ip": device.tray_snmp_ip or "192.168.1.115",
                "snmp_port": device.snmp_port or 161,
                "snmp_version": device.snmp_version or "v2c",
                "community": device.snmp_community or "public",
            }, indent=2)

    # ------------------------------------------------------------------
    # Token lifecycle
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        devices = super().create(vals_list)
        for device in devices.sudo().filtered(lambda d: d.is_tray_printer and not d.push_token):
            device.push_token = secrets.token_urlsafe(24)
        return devices

    def write(self, vals):
        res = super().write(vals)
        # Make sure any device turned into a tray printer has a push token.
        if vals.get("is_tray_printer"):
            for device in self.sudo().filtered(lambda d: d.is_tray_printer and not d.push_token):
                device.push_token = secrets.token_urlsafe(24)
        return res

    def action_regenerate_token(self):
        """Rotate a (possibly leaked) push token. Manager action."""
        for device in self:
            device.sudo().push_token = secrets.token_urlsafe(24)
        return True

    # ------------------------------------------------------------------
    # Polling (direct mode)
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        if self.tray_connection_mode == "agent":
            raise UserError(_(
                "This printer is in 'Agent push' mode, so Odoo does not poll it. "
                "Run the IoT-box agent and the trays will appear here."
            ))
        self._poll_one(raise_on_error=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("SNMP OK"),
                "message": _("Connected. %s tray(s) read.") % len(self.tray_ids),
                "type": "success", "sticky": False,
            },
        }

    def action_poll_now(self):
        for device in self.filtered(lambda d: d.is_tray_printer and d.tray_connection_mode == "direct"):
            device._poll_one(raise_on_error=False)
        return True

    def _poll_one(self, raise_on_error=False):
        self.ensure_one()
        if not self.tray_snmp_ip:
            if raise_on_error:
                raise UserError(_("Set the Printer IP first."))
            return
        try:
            names = dict(self._walk(self.oid_tray_name or DEFAULT_OID_NAME))
            levels = dict(self._walk(self.oid_current_level or DEFAULT_OID_LEVEL))
            maxima = dict(self._walk(self.oid_max_capacity or DEFAULT_OID_MAX))
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({
                "last_status": "error", "last_error": str(exc),
                "last_poll": fields.Datetime.now(),
            })
            _logger.warning("Printer %s SNMP poll failed: %s", self.name, exc)
            if raise_on_error:
                raise UserError(_("SNMP poll failed:\n%s") % exc) from exc
            return

        trays = [
            {
                "index": str(index),
                "name": names.get(index) or _("Tray %s") % index,
                "current": levels.get(index),
                "max": maxima.get(index),
            }
            for index in sorted(levels)
        ]
        self._apply_tray_data(trays)
        self.sudo().write({
            "last_status": "ok", "last_error": False,
            "last_poll": fields.Datetime.now(),
        })

    def _walk(self, oid):
        return snmp_client.walk(
            self.tray_snmp_ip, self.snmp_port or 161, self.snmp_community or "public",
            self.snmp_version or "v2c", oid,
        )

    # ------------------------------------------------------------------
    # Shared upsert (used by direct poll and agent push)
    # ------------------------------------------------------------------
    def _apply_tray_data(self, trays):
        """Upsert tray rows. ``trays`` is a list of dicts: index/name/current/max."""
        self.ensure_one()
        if len(trays) > MAX_TRAYS:
            raise UserError(_("Too many trays in one update (max %s).") % MAX_TRAYS)
        Tray = self.env["printer.tray"].sudo()
        existing = {t.tray_index: t for t in self.tray_ids}
        seen = set()
        for data in trays:
            index = str(data.get("index"))
            seen.add(index)
            vals = {
                "name": data.get("name") or _("Tray %s") % index,
                "current_level": _as_int(data.get("current")),
                "max_capacity": _as_int(data.get("max")),
                "last_update": fields.Datetime.now(),
            }
            if index in existing:
                existing[index].write(vals)
            else:
                vals.update({"device_id": self.id, "tray_index": index})
                Tray.create(vals)
        stale = self.tray_ids.filtered(lambda t: t.tray_index not in seen)
        if stale:
            stale.unlink()

    @api.model
    def _cron_poll_all(self):
        devices = self.search([
            ("is_tray_printer", "=", True),
            ("tray_connection_mode", "=", "direct"),
        ])
        for device in devices:
            device._poll_one(raise_on_error=False)
            self.env.cr.commit()  # isolate one bad printer from the rest

    def action_view_trays(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Trays - %s") % self.name,
            "res_model": "printer.tray",
            "view_mode": "list,form",
            "domain": [("device_id", "=", self.id)],
            "context": {"default_device_id": self.id},
        }


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return -2  # Printer-MIB "unknown"
