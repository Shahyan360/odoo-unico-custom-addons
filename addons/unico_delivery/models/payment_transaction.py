from odoo import models


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    def _send_invoice(self):
        """Skip the automatic-invoice mail when customer emails are off.

        Core mails the freshly generated invoice straight after an online
        payment (and again from ``sale.send_invoice_cron``). Odoo's own
        ``sale.automatic_invoice`` switch also controls whether the invoice is
        *created*; this only stops it being *mailed*, so the invoice is still
        raised and can be sent by hand.
        """
        allowed = self.filtered(
            lambda tx: tx.company_id.unico_send_automatic_customer_emails
        )
        return super(PaymentTransaction, allowed)._send_invoice()
