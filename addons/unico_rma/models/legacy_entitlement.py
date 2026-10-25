from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class RmaLegacyEntitlement(models.Model):
    _name = "rma.legacy.entitlement"
    _description = "Imported Sage Sale Line"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "sage_line_ref"
    _order = "invoice_date desc, id desc"

    _sage_line_company_unique = models.Constraint(
        "unique(company_id, sage_line_ref)",
        "Sage line ID must be unique per company.",
    )
    _qty_non_negative = models.Constraint(
        "CHECK(qty_sold >= 0 AND qty_returned >= 0)",
        "Sold and returned quantities cannot be negative.",
    )

    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        index=True,
    )
    product_id = fields.Many2one(
        "product.product",
        required=True,
        index=True,
    )
    sage_order_ref = fields.Char("Sage Order #", index=True)
    sage_invoice_ref = fields.Char("Sage Invoice #", index=True)
    sage_line_ref = fields.Char(
        "Sage Line ID",
        required=True,
        index=True,
        help="Stable imported Sage line key. Use invoice+line if Sage has no single line ID.",
    )
    invoice_date = fields.Date("Sage Invoice Date")
    migration_cutoff_date = fields.Date(
        "Migration Cutoff Date",
        required=True,
        help=(
            "For imports, the last Sage transaction date included in the file. "
            "For manager-created gap-fill rows, the date the original Sage sale was verified."
        ),
    )
    qty_sold = fields.Float("Qty Sold", required=True)
    qty_returned = fields.Float("Qty Already Returned", default=0.0, tracking=True)
    qty_available = fields.Float(
        "Qty Left to Return",
        compute="_compute_qty_available",
        store=True,
    )
    price_unit = fields.Float("Unit Price")
    unit_cost = fields.Float("Unit Cost")
    refund_account_id = fields.Many2one(
        "account.account",
        string="Credit Account",
        domain=[
            (
                "account_type",
                "not in",
                ("asset_receivable", "liability_payable", "asset_cash", "liability_credit_card"),
            )
        ],
    )
    tax_code = fields.Char("Sage Tax Code")
    tax_ids = fields.Many2many(
        "account.tax",
        "rma_legacy_entitlement_tax_rel",
        "entitlement_id",
        "tax_id",
        string="Taxes to Use on Credit",
        domain=[("type_tax_use", "=", "sale")],
    )
    lot_id = fields.Many2one(
        "stock.lot",
        string="Original Lot/Serial",
        domain="[('product_id', '=', product_id)]",
    )
    imported_filename = fields.Char("Imported File")
    import_note = fields.Text("Import Notes")
    rma_line_ids = fields.One2many(
        "rma.lines",
        "legacy_entitlement_id",
        string="RMA Lines",
        readonly=True,
    )

    @api.depends("qty_sold", "qty_returned")
    def _compute_qty_available(self):
        for entitlement in self:
            entitlement.qty_available = max(
                entitlement.qty_sold - entitlement.qty_returned,
                0.0,
            )

    @api.constrains("qty_sold", "qty_returned")
    def _check_returned_within_sold(self):
        for entitlement in self:
            if entitlement.qty_returned > entitlement.qty_sold:
                raise ValidationError(
                    _("Qty Already Returned cannot be greater than Qty Sold for %s.")
                    % entitlement.display_name
                )

    @api.depends("sage_line_ref", "sage_invoice_ref", "product_id")
    def _compute_display_name(self):
        for entitlement in self:
            label = "[%s] %s" % (
                entitlement.sage_line_ref,
                entitlement.product_id.display_name,
            )
            if entitlement.sage_invoice_ref:
                label = "%s - %s" % (entitlement.sage_invoice_ref, label)
            entitlement.display_name = label

    def action_view_rma_lines(self):
        self.ensure_one()
        return {
            "name": _("RMA Lines"),
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "rma.lines",
            "domain": [("legacy_entitlement_id", "=", self.id)],
            "context": {"create": False},
        }

    def consume_return_qty(self, qty):
        self.ensure_one()
        if qty <= 0:
            return
        self.lock_for_update(allow_referencing=True)
        self.invalidate_recordset(["qty_returned", "qty_available"])
        available = self.qty_available
        if qty > available:
            raise ValidationError(
                _(
                    "Original Sage sale line %(line)s only has %(available)s left to return; "
                    "%(qty)s was requested."
                )
                % {
                    "line": self.display_name,
                    "available": available,
                    "qty": qty,
                }
            )
        self.qty_returned += qty

    def release_return_qty(self, qty):
        self.ensure_one()
        if qty <= 0:
            return
        self.lock_for_update(allow_referencing=True)
        self.invalidate_recordset(["qty_returned"])
        self.qty_returned = max(self.qty_returned - qty, 0.0)
