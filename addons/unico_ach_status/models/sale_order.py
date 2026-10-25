from odoo import api, fields, models

# Stripe's ACH Direct Debit payment method. Cards settle immediately; ACH sits
# in `pending` for days, which is exactly the window this report exposes.
ACH_PAYMENT_METHOD_CODE = 'ach_direct_debit'

# `pending` is money submitted to the bank but not cleared. `authorized` is
# included for completeness -- it is money held but not captured, which is the
# same "not really in yet" situation for the counter.
ACH_UNCLEARED_STATES = ('pending', 'authorized')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    unico_ach_pending_amount = fields.Monetary(
        string="ACH Not Cleared",
        compute='_compute_unico_ach_pending',
        store=True,
        currency_field='currency_id',
        help="Total of ACH Direct Debit payments on this order that Stripe has "
             "not confirmed yet.",
    )
    unico_ach_pending_since = fields.Datetime(
        string="ACH Submitted On",
        compute='_compute_unico_ach_pending',
        store=True,
        help="When the oldest uncleared ACH payment on this order was started.",
    )
    unico_ach_days_pending = fields.Integer(
        string="Days Pending",
        compute='_compute_unico_ach_days_pending',
        help="Days since the oldest uncleared ACH payment was started. ACH "
             "normally clears in 4-5 business days.",
    )

    @api.depends(
        'transaction_ids.state',
        'transaction_ids.amount',
        'transaction_ids.create_date',
        'transaction_ids.payment_method_id',
    )
    def _compute_unico_ach_pending(self):
        for order in self:
            # payment.transaction is readable by system users only, so the
            # salesperson opening this list would otherwise hit an AccessError.
            uncleared = order.sudo().transaction_ids.filtered(
                lambda tx: (
                    tx.state in ACH_UNCLEARED_STATES
                    and tx.payment_method_id.code == ACH_PAYMENT_METHOD_CODE
                )
            )
            order.unico_ach_pending_amount = sum(uncleared.mapped('amount'))
            create_dates = [tx.create_date for tx in uncleared if tx.create_date]
            order.unico_ach_pending_since = min(create_dates) if create_dates else False

    @api.depends('unico_ach_pending_since')
    def _compute_unico_ach_days_pending(self):
        now = fields.Datetime.now()
        for order in self:
            if order.unico_ach_pending_since:
                order.unico_ach_days_pending = (now - order.unico_ach_pending_since).days
            else:
                order.unico_ach_days_pending = 0
