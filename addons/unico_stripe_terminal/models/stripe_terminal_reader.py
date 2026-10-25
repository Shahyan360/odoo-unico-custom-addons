import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import stripe_client

_logger = logging.getLogger(__name__)


class StripeTerminalReader(models.Model):
    _name = "stripe.terminal.reader"
    _description = "Stripe Terminal Reader"
    _rec_name = "label"
    _order = "label"

    label = fields.Char(string="Label", required=True)
    stripe_reader_id = fields.Char(string="Stripe Reader ID", index=True)
    device_type = fields.Char(string="Device Type")
    serial_number = fields.Char(string="Serial Number")
    status = fields.Selection(
        selection=[("online", "Online"), ("offline", "Offline")],
        string="Status",
        default="offline",
    )
    is_simulated = fields.Boolean(string="Simulated")
    location_id = fields.Many2one(
        comodel_name="stripe.terminal.location",
        string="Location",
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )

    _stripe_reader_uniq = models.Constraint(
        "unique(stripe_reader_id, company_id)",
        "This Stripe reader is already registered for this company.",
    )

    @api.depends("label", "status", "stripe_reader_id")
    def _compute_display_name(self):
        for reader in self:
            name = reader.label or reader.stripe_reader_id or _("Reader")
            if reader.status == "offline":
                name = "%s (%s)" % (name, _("offline"))
            reader.display_name = name

    @api.model
    def _upsert_from_stripe(self, stripe_reader, company):
        """Create or update a reader record from a Stripe Reader object."""
        sget = stripe_client.sget
        location = False
        stripe_location_id = sget(stripe_reader, "location")
        if stripe_location_id:
            location = self.env["stripe.terminal.location"].search([
                ("stripe_location_id", "=", stripe_location_id),
                ("company_id", "=", company.id),
            ], limit=1)
        values = {
            "label": sget(stripe_reader, "label") or sget(stripe_reader, "id"),
            "stripe_reader_id": sget(stripe_reader, "id"),
            "device_type": sget(stripe_reader, "device_type"),
            "serial_number": sget(stripe_reader, "serial_number"),
            "status": "online" if sget(stripe_reader, "status") == "online" else "offline",
            "location_id": location.id if location else False,
            "company_id": company.id,
        }
        existing = self.search([
            ("stripe_reader_id", "=", values["stripe_reader_id"]),
            ("company_id", "=", company.id),
        ], limit=1)
        if existing:
            existing.write(values)
            return existing
        return self.create(values)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_sync_status(self):
        for reader in self:
            stripe, dummy = stripe_client.get_stripe_for_company(
                self.env, reader.company_id)
            try:
                data = stripe.terminal.Reader.retrieve(reader.stripe_reader_id)
            except Exception as ex:
                _logger.exception("Reader status sync failed")
                raise UserError(_("Could not retrieve reader: %s", ex))
            reader._upsert_from_stripe(data, reader.company_id)
        return True

    def action_cancel_action(self):
        """Cancel whatever the reader is currently doing."""
        self.ensure_one()
        stripe, dummy = stripe_client.get_stripe_for_company(
            self.env, self.company_id)
        try:
            stripe.terminal.Reader.cancel_action(self.stripe_reader_id)
        except Exception as ex:
            _logger.exception("Reader cancel_action failed")
            raise UserError(_("Could not cancel the reader action: %s", ex))
        return True
