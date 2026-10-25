import logging
from odoo import fields, models, api, _
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

log = logging.getLogger(__name__).info


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    rma_id = fields.Many2one('rma.main', 'RMA Id', copy=False)

    rma_count = fields.Integer(
        string="RMA",
        copy=False,
        compute='_compute_rma_count'
    )

    rma_credit_available_amount = fields.Monetary(
        string="RMA Credit Available",
        compute='_compute_rma_credit_available_amount',
        currency_field='currency_id',
        help="Outstanding customer credit released by return-with-credit "
             "RMAs on this order that is still available to apply to a new "
             "invoice.",
    )

    def _compute_rma_credit_available_amount(self):
        for order in self:
            if not isinstance(order.id, int):
                order.rma_credit_available_amount = 0.0
                continue
            rmas = self.env['rma.main'].search([('sale_order', '=', order.id)])
            order.rma_credit_available_amount = sum(
                rmas.mapped('credit_available_amount')
            )


    def _compute_rma_count(self):
        for order in self:
            # Unsaved orders (NewId, e.g. a quotation being edited before save)
            # can have no RMA yet and aren't valid domain values.
            if not isinstance(order.id, int):
                order.rma_count = 0
                continue
            order.rma_count = self.env['rma.main'].search_count(
                [('sale_order', '=', order.id)])

    def _create_invoices(self, grouped=False, final=False, date=None):
        invoices = super(SaleOrder, self)._create_invoices(grouped=grouped, final=final, date=date)
        if len(self) == 1:
            # A singleton error occurs when trying to create multiples invoice at the same time.
            #   Given that there is already a mechanism to prevent the creation of an RMA if the sale order is not yet
            #   invoiced, we can safely assume that when creating multiple invoices
            #   at the same time, we are not in a context where an RMA can be created,
            #   and thus we can skip the assignment of the rma_id on the credit notes.

            rma_id = self.env['rma.main'].search([('sale_order', '=', self.id)], order='id desc', limit=1)

            for invoice in invoices:
                if rma_id and invoice.move_type == 'out_refund':
                    invoice.rma_id = rma_id.id
        return invoices

    def action_view_rma_orders(self):
        """This function displays the list view of all the RMAs related to the current Sale Order.

        Returns:
            _type_: View
        """
        rma_ids = self.env['rma.main'].search([('sale_order', '=', self.id)]).ids

        return {
            "name": "RMA Orders",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "rma.main",
            "domain": [("id", "in", rma_ids)],
        }

    def extract_ids_from_domain(self, domain):
        for item in domain:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                field, operator, value = item
                if field == 'id' and operator == 'in':
                    return value
        return None


    @api.readonly
    def action_view_invoice(self, invoices=False):
        """Add the ids credit notes created by the RMA process

        Args:
            invoices (bool, optional): _description_. Defaults to False.

        Returns:
            _type_: _description_
        """
        res = super(SaleOrder, self).action_view_invoice(invoices=invoices)

        if len(self) > 1:
            return res
        rma_id = self.env['rma.main'].search([('sale_order', '=', self.id)], order='id desc', limit=1)
        rma_invoice_ids = self.env['account.move'].search([
            ('rma_id', '=', rma_id.id),
            ('move_type', '=', 'out_refund'),
            ('sale_id', '=', self.id)
        ])

        if rma_invoice_ids:
            invoices = self.mapped('invoice_ids')
            invoice_ids = list(set(invoices.ids) | set(rma_invoice_ids.ids))

            res['domain'] = [('id', 'in', invoice_ids)]
            res.pop('res_id')
            res.pop('views')
            res.pop('view_id')
            res['view_mode'] = 'list,form'

        return res

    @api.depends('order_line.invoice_lines')
    def _get_invoiced(self):
        """Override to include RMA lines in invoiced computation
        """
        super(SaleOrder, self)._get_invoiced()
        for order in self:
            # Skip unsaved orders (NewId): no RMA exists and it's not a valid
            # domain value.
            if not isinstance(order.id, int):
                continue
            rma_id = self.env['rma.main'].search([('sale_order', '=', order.id)], order='id desc', limit=1)
            if rma_id:
                rma_invoice_ids = self.env['account.move'].search([('rma_id', '=', rma_id.id), ('move_type', '=', 'out_refund')])
                invoices = order.order_line.invoice_lines.move_id.filtered(lambda r: r.move_type in ('out_invoice', 'out_refund'))
                total_invoices = invoices | rma_invoice_ids
                order.invoice_count = len(total_invoices)

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    return_order_line_id = fields.One2many('rma.lines', 'sale_line_id')

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    rma_line_id = fields.Many2one('rma.lines')
    stock_move_id = fields.Many2one('stock.move')

class AccountMove(models.Model):
    _inherit = 'account.move'

    rma_id = fields.Many2one('rma.main', 'RMA Id')
    rma_line_id = fields.Many2one('rma.lines', 'RMA Line Id.')
    picking_id = fields.Many2one('stock.picking','Picking')
    sale_id  =  fields.Many2one('sale.order', 'Sale Origin')

    def action_post(self):
        blocked_legacy_credits = self.filtered(
            lambda move: move.move_type == "out_refund"
            and move.rma_id.rma_type == "legacy_sage"
            and not move.rma_id.legacy_accounting_approved
        )
        if blocked_legacy_credits:
            raise UserError(_(
                "Accounting approval is required before posting a legacy Sage credit note."
            ))
        return super().action_post()
