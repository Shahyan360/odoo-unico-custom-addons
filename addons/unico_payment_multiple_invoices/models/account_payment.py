from odoo import api, fields, models

# A payment is settled through its counterpart journal item — the one on the
# receivable (customer) or payable (vendor) account. That line's residual is
# what is left over after the payment has been matched against invoices or
# bills, so it is the honest answer to "how much of this money is still free
# to allocate". Payments that never posted have no such line.
UNALLOCATABLE_STATES = ("draft", "canceled", "rejected")


class AccountPayment(models.Model):
    _inherit = "account.payment"

    unico_amount_unallocated = fields.Monetary(
        string="Remaining Balance",
        compute="_compute_unico_allocation",
        store=True,
        currency_field="currency_id",
        help="How much of this payment is still free to apply to another "
             "invoice or bill. A customer who paid 500 with 300 applied to an "
             "order still has 200 remaining; the same reading applies to a "
             "vendor payment against bills.",
    )

    @api.depends(
        "move_id.line_ids.amount_residual",
        "move_id.line_ids.amount_residual_currency",
        "move_id.line_ids.account_id",
        "amount",
        "state",
    )
    def _compute_unico_allocation(self):
        for payment in self:
            payment.unico_amount_unallocated = 0.0
            if payment.state in UNALLOCATABLE_STATES:
                # Nothing is committed yet (or it was undone), so the figure
                # means nothing — showing the full amount on a cancelled
                # payment would read as money waiting to be used.
                continue
            if not payment.move_id or not payment.currency_id:
                continue

            _liquidity_lines, counterpart_lines, writeoff_lines = (
                payment._seek_for_lines()
            )
            reconcile_lines = (counterpart_lines + writeoff_lines).filtered(
                lambda line: line.account_id.reconcile
            )
            if not reconcile_lines:
                continue

            # Same currency choice core makes in _compute_reconciliation_status:
            # the residual in company currency is only meaningful when the
            # payment is in that currency.
            residual_field = (
                "amount_residual"
                if payment.currency_id == payment.company_id.currency_id
                else "amount_residual_currency"
            )
            # Receivable lines are credits and payable lines debits, so the
            # raw residual is signed by direction; the amount on the payment
            # is always positive and these should read the same way.
            payment.unico_amount_unallocated = abs(
                sum(reconcile_lines.mapped(residual_field))
            )
