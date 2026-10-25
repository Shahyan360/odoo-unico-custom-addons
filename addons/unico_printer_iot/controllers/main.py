"""HTTP endpoint used by the Windows IoT-box agent (Agent-push mode).

The agent does the SNMP walk on the LAN and POSTs the tray values here, so
Odoo never needs direct network access to the printer.

POST /printer_iot/push   (Content-Type: application/json)
{
  "token": "<device push_token>",
  "trays": [
    {"index": "1.1", "name": "Cassette 1", "current": 0,   "max": 500},
    {"index": "1.2", "name": "Cassette 2", "current": 500, "max": 500}
  ]
}
"""
import logging

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PrinterIotController(http.Controller):

    # Mirror of the model cap; reject oversized payloads before any DB work.
    MAX_TRAYS = 64

    @http.route("/printer_iot/push", type="jsonrpc", auth="public", methods=["POST"], csrf=False)
    def push(self, token=None, trays=None, **kw):
        trays = trays or []
        if not token:
            return {"ok": False, "error": "missing token"}
        if not isinstance(trays, list) or len(trays) > self.MAX_TRAYS:
            return {"ok": False, "error": "invalid payload"}

        device = request.env["iot.device"].sudo().search(
            [("push_token", "=", token), ("is_tray_printer", "=", True)], limit=1
        )
        if not device:
            _logger.warning("printer_iot push rejected: unknown token")
            return {"ok": False, "error": "invalid token"}

        try:
            device._apply_tray_data(trays)
            device.write({
                "last_status": "ok",
                "last_error": False,
                "last_poll": fields.Datetime.now(),
            })
        except Exception:  # noqa: BLE001
            # Log detail server-side; return a generic message to the caller.
            device.sudo().write({"last_status": "error", "last_error": "push failed"})
            _logger.exception("printer_iot push failed for %s", device.name)
            return {"ok": False, "error": "processing error"}

        return {"ok": True, "device": device.name, "trays": len(trays)}
