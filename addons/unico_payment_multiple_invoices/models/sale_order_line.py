# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

# Inverting the tax engine is exact for percentage taxes; the extra passes only
# absorb the rounding it applies, so convergence never needs more than a few.
LINE_TOTAL_SOLVER_PASSES = 5


class SaleOrderLine(models.Model):
    """Let Sales negotiate a line total instead of a unit price.

    Sales agrees a figure for the whole line, then has to work backwards to a
    unit price by hand. ``price_subtotal`` is computed from ``price_unit``, so
    making it writable would fight Odoo's compute chain and leave margin stale.
    These helpers invert the computation instead: the entered total is turned
    into a unit price, and margin follows from Odoo's own computes.
    """

    _inherit = 'sale.order.line'

    unico_can_set_line_total = fields.Boolean(
        string="Can Set Line Total",
        compute='_compute_unico_can_set_line_total',
        help="Technical UI flag for the Set Line Total button.",
    )

    def _get_line_total_currency(self):
        self.ensure_one()
        return (
            self.currency_id
            or self.order_id.currency_id
            or self.company_id.currency_id
            or self.env.company.currency_id
        )

    def _is_line_total_price_writable(self):
        """Return whether a direct Unit Price write is accepted right now.

        Extension point for modules that gate Unit Price edits behind their own
        workflow, so the button is never offered for a price that Apply would
        be refused. The base rules already live in
        :meth:`_is_line_total_editable`.
        """
        self.ensure_one()
        return True

    def _is_line_total_editable(self):
        """Return whether Set Line Total may back-solve this line's price."""
        self.ensure_one()
        if self.display_type or self.is_downpayment:
            return False
        if self.state not in ('draft', 'sent') or self.qty_invoiced:
            return False
        if self.order_id.locked:
            return False
        if self.product_uom_qty <= 0:
            return False
        return self._is_line_total_price_writable()

    @api.depends(
        'display_type',
        'is_downpayment',
        'state',
        'qty_invoiced',
        'product_uom_qty',
        'order_id.locked',
    )
    def _compute_unico_can_set_line_total(self):
        for line in self:
            line.unico_can_set_line_total = line._is_line_total_editable()

    def _line_subtotal_for_price_unit(self, price_unit, discount=0.0):
        """Return the pre-tax subtotal Odoo computes for a candidate price."""
        self.ensure_one()
        AccountTax = self.env['account.tax']
        company = self.company_id or self.order_id.company_id or self.env.company
        base_line = self._prepare_base_line_for_taxes_computation(
            price_unit=price_unit,
            discount=discount,
        )
        AccountTax._add_tax_details_in_base_line(base_line, company)
        AccountTax._round_base_lines_tax_details([base_line], company)
        return base_line['tax_details']['total_excluded_currency']

    def _price_unit_for_line_subtotal(self, target_subtotal, discount=0.0):
        """Back-solve the Unit Price that produces ``target_subtotal``.

        Ask Odoo's own tax engine rather than dividing by quantity: with
        price-included or fixed-amount taxes the subtotal is not simply
        ``price_unit * qty``. That computation is affine in ``price_unit``, so
        two probes invert it exactly; the refinement passes then absorb the
        rounding the engine applies.

        The result is rounded to the stored Product Price precision, so a
        caller can trust that applying it reproduces the previewed subtotal.
        """
        self.ensure_one()
        currency = self._get_line_total_currency()
        if self.product_uom_qty <= 0:
            raise UserError(_(
                "Set a quantity on %(line)s before entering a line total.",
                line=self.display_name or self.name or '',
            ))
        price_digits = self.env['decimal.precision'].precision_get('Product Price')
        target_subtotal = currency.round(target_subtotal or 0.0)
        base = self._line_subtotal_for_price_unit(0.0, discount=discount)
        slope = self._line_subtotal_for_price_unit(1.0, discount=discount) - base
        if not slope:
            raise UserError(_(
                "The subtotal of %(line)s does not vary with its Unit Price, so "
                "a line total cannot be back-solved.",
                line=self.display_name or self.name or '',
            ))
        price_unit = float_round(
            (target_subtotal - base) / slope, precision_digits=price_digits,
        )
        for _attempt in range(LINE_TOTAL_SOLVER_PASSES):
            gap = target_subtotal - self._line_subtotal_for_price_unit(
                price_unit, discount=discount,
            )
            if currency.is_zero(gap):
                break
            price_unit = float_round(
                price_unit + gap / slope, precision_digits=price_digits,
            )
        return price_unit

    def _apply_line_total(self, price_unit):
        """Pin ``price_unit`` so the line charges the negotiated total.

        The discount is cleared, otherwise the entered figure is not the
        pre-tax charge. It is only written when there is one to clear, because
        a module that pins a negotiated price may forbid discount edits while
        that price is active.
        """
        self.ensure_one()
        if not self._is_line_total_editable():
            raise UserError(_(
                "The total of %(line)s can no longer be changed.",
                line=self.display_name or self.name or '',
            ))
        values = {'price_unit': price_unit}
        if self.discount:
            values['discount'] = 0.0
        self.write(values)
        return True

    def action_open_line_total_wizard(self):
        """Open the popup that turns a negotiated line total into a price."""
        self.ensure_one()
        if not self._is_line_total_editable():
            raise UserError(_(
                "The total of %(line)s can no longer be changed. A line total "
                "applies to an unlocked, uninvoiced draft or sent quotation.",
                line=self.display_name or self.name or '',
            ))
        wizard = self.env['unico.line.total.wizard'].create({
            'order_line_id': self.id,
            'current_price_unit': self.price_unit,
            'current_discount': self.discount,
            'current_subtotal': self.price_subtotal,
            'current_margin': self.margin,
            'current_margin_percent': self.margin_percent,
            'unit_cost': self.purchase_price,
            'target_subtotal': self.price_subtotal,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Set Line Total"),
            'res_model': 'unico.line.total.wizard',
            'view_mode': 'form',
            # ``views`` is only derived from ``view_mode`` by the call_button
            # controller. This action is fetched over a plain ORM call, and the
            # web client reads ``action.views`` without a fallback, so state it.
            'views': [(False, 'form')],
            'res_id': wizard.id,
            'target': 'new',
        }
