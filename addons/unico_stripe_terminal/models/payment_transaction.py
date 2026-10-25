from odoo import _, models
from odoo.exceptions import UserError


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    def action_manual_confirm_payment(self):
        """Open the wizard to manually confirm a still-pending transaction.

        Used for ACH (and occasionally card) payments that sit in ``pending``
        until the bank clears them. When the customer is trusted, the payment can
        be confirmed manually (with a mandatory remark) ahead of clearing so the
        material is released and the money is booked against the order.
        """
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_(
                "Only a pending payment transaction can be manually confirmed "
                "(this one is %s).", self.state))
        return {
            "type": "ir.actions.act_window",
            "name": _("Manually Confirm Payment"),
            "res_model": "payment.transaction.manual.confirm",
            "view_mode": "form",
            "target": "new",
            "context": {"default_transaction_id": self.id},
        }

    def _manual_confirm_payment(self, reason):
        """Confirm a pending transaction through the standard processing path.

        Drives the same underlying flow a provider webhook would: set the
        transaction to ``done`` (recording the remark as the state message) and
        run post-processing, which creates and posts the ``account.payment`` and
        validates any linked invoice. Finally logs who confirmed it, and why, on
        every linked sales order's chatter.
        """
        self.ensure_one()
        if self.state != "pending":
            raise UserError(_(
                "Only a pending payment transaction can be manually confirmed "
                "(this one is %s).", self.state))
        tx = self.sudo()
        message = _("Manually confirmed in Odoo by %(user)s. Reason: %(reason)s",
                    user=self.env.user.display_name, reason=reason)
        tx._set_done(state_message=message)
        tx._post_process()
        body = _(
            "Payment transaction %(ref)s (%(amount)s %(currency)s) was manually "
            "confirmed by %(user)s.<br/>Reason: %(reason)s",
            ref=tx.reference,
            amount=tx.amount,
            currency=tx.currency_id.name,
            user=self.env.user.display_name,
            reason=reason,
        )
        for order in tx.sale_order_ids:
            order.message_post(body=body)
        return True
