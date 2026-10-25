# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class LineTotalWizard(models.TransientModel):
    """Turn a negotiated line total into a Unit Price.

    Sales negotiates a total for the line, not a unit price. Making
    ``price_subtotal`` writable would fight Odoo's compute chain, so this
    wizard back-solves the Unit Price instead and previews the resulting
    margin before anything is written.
    """

    _name = 'unico.line.total.wizard'
    _description = 'Set Sale Order Line Total'

    order_line_id = fields.Many2one(
        'sale.order.line',
        string="Order Line",
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    order_id = fields.Many2one(related='order_line_id.order_id')
    currency_id = fields.Many2one(related='order_line_id.currency_id')
    product_uom_qty = fields.Float(
        related='order_line_id.product_uom_qty',
        string="Quantity",
    )

    current_price_unit = fields.Monetary(string="Current Unit Price", readonly=True)
    current_subtotal = fields.Monetary(string="Current Line Total", readonly=True)
    current_margin = fields.Monetary(string="Current Margin", readonly=True)
    current_margin_percent = fields.Float(string="Current Margin (%)", readonly=True)
    # Kept off the form: the discount only drives the warning about clearing it,
    # and the cost is an input to the new margin rather than something to read.
    current_discount = fields.Float(string="Current Discount %", readonly=True)
    unit_cost = fields.Monetary(string="Unit Cost", readonly=True)

    target_subtotal = fields.Monetary(
        string="New Line Total",
        required=True,
        help="Pre-tax total this line should charge. Unit Price is derived from it.",
    )

    new_price_unit = fields.Monetary(
        string="New Unit Price",
        compute='_compute_line_total_preview',
    )
    new_subtotal = fields.Monetary(
        string="Resulting Line Total",
        compute='_compute_line_total_preview',
    )
    new_margin = fields.Monetary(
        string="New Margin",
        compute='_compute_line_total_preview',
    )
    new_margin_percent = fields.Float(
        string="New Margin (%)",
        compute='_compute_line_total_preview',
    )
    rounding_notice = fields.Char(compute='_compute_line_total_preview')

    @api.depends('target_subtotal', 'order_line_id')
    def _compute_line_total_preview(self):
        for wizard in self:
            line = wizard.order_line_id
            currency = wizard.currency_id or line.currency_id
            price_unit = new_subtotal = 0.0
            notice = False
            if line and line._is_line_total_editable():
                price_unit = line._price_unit_for_line_subtotal(wizard.target_subtotal)
                new_subtotal = line._line_subtotal_for_price_unit(price_unit)
                if currency and currency.compare_amounts(
                    new_subtotal, wizard.target_subtotal or 0.0
                ):
                    # Unit Price cannot always divide the total exactly, e.g. a
                    # quantity of 3 against a round figure. Show the charge that
                    # will actually be stored rather than the one requested.
                    notice = _(
                        "Unit Price rounding puts the line total at %(actual)s "
                        "instead of %(requested)s.",
                        actual=currency.format(new_subtotal),
                        requested=currency.format(wizard.target_subtotal or 0.0),
                    )
            margin = new_subtotal - (wizard.unit_cost or 0.0) * (wizard.product_uom_qty or 0.0)
            wizard.new_price_unit = price_unit
            wizard.new_subtotal = new_subtotal
            wizard.new_margin = margin
            # Native ``margin_percent`` stores a fraction, not a percentage.
            wizard.new_margin_percent = margin / new_subtotal if new_subtotal else 0.0
            wizard.rounding_notice = notice

    def action_apply(self):
        self.ensure_one()
        if not self.order_line_id:
            raise UserError(_("This wizard is no longer attached to an order line."))
        self.order_line_id._apply_line_total(self.new_price_unit)
        return {'type': 'ir.actions.client', 'tag': 'reload'}
