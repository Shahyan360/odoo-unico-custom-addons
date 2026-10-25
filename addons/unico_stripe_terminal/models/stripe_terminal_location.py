from odoo import api, fields, models

from . import stripe_client


class StripeTerminalLocation(models.Model):
    _name = "stripe.terminal.location"
    _description = "Stripe Terminal Location"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    stripe_location_id = fields.Char(string="Stripe Location ID", required=True, index=True)
    address_line1 = fields.Char(string="Address")
    address_city = fields.Char(string="City")
    address_postal_code = fields.Char(string="ZIP")
    address_state = fields.Char(string="State")
    address_country = fields.Char(string="Country")
    reader_ids = fields.One2many(
        comodel_name="stripe.terminal.reader",
        inverse_name="location_id",
        string="Readers",
    )
    default_reader_id = fields.Many2one(
        comodel_name="stripe.terminal.reader",
        string="Default Reader",
        domain="[('id', 'in', reader_ids)]",
        ondelete="set null",
        help="Reader pre-selected in the Collect Payment popup when this "
             "location is used.",
    )
    reader_count = fields.Integer(compute="_compute_reader_count")
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )

    _stripe_location_uniq = models.Constraint(
        "unique(stripe_location_id, company_id)",
        "This Stripe location is already registered for this company.",
    )

    @api.depends("reader_ids")
    def _compute_reader_count(self):
        for location in self:
            location.reader_count = len(location.reader_ids)

    @api.model
    def _upsert_from_stripe(self, stripe_location, company):
        """Create or update a location record from a Stripe Location object."""
        sget = stripe_client.sget
        address = sget(stripe_location, "address") or {}
        values = {
            "name": sget(stripe_location, "display_name") or sget(stripe_location, "id"),
            "stripe_location_id": sget(stripe_location, "id"),
            "address_line1": sget(address, "line1"),
            "address_city": sget(address, "city"),
            "address_postal_code": sget(address, "postal_code"),
            "address_state": sget(address, "state"),
            "address_country": sget(address, "country"),
            "company_id": company.id,
        }
        existing = self.search([
            ("stripe_location_id", "=", values["stripe_location_id"]),
            ("company_id", "=", company.id),
        ], limit=1)
        if existing:
            existing.write(values)
            return existing
        return self.create(values)
