from odoo import _, fields, models
from odoo.exceptions import UserError


class PaymentTransactionManualConfirm(models.TransientModel):
    _name = "payment.transaction.manual.confirm"
    _description = "Manually Confirm Pending Payment Transaction"

    transaction_id = fields.Many2one(
        comodel_name="payment.transaction",
        string="Payment Transaction",
        required=True,
        readonly=True,
        default=lambda self: self.env.context.get("default_transaction_id"),
    )
    reference = fields.Char(related="transaction_id.reference", string="Reference")
    amount = fields.Monetary(related="transaction_id.amount", string="Amount")
    currency_id = fields.Many2one(related="transaction_id.currency_id")
    partner_id = fields.Many2one(
        related="transaction_id.partner_id", string="Customer")
    reason = fields.Text(
        string="Remark",
        required=True,
        help="Why this pending payment is being confirmed before the provider "
             "reports it as cleared. Recorded on the transaction and posted to "
             "the linked sales order's chatter.",
    )

    def action_confirm(self):
        self.ensure_one()
        if not (self.reason and self.reason.strip()):
            raise UserError(_("Please enter a remark explaining the manual confirmation."))
        self.transaction_id._manual_confirm_payment(self.reason.strip())
        return {"type": "ir.actions.act_window_close"}
