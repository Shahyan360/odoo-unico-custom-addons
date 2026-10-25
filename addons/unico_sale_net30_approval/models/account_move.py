import logging

from odoo import _, models

_logger = logging.getLogger(__name__)

# Code-versioned mail template (see data/net30_invoice_email.xml). Carries the
# invoice PDF and a portal link the customer can pay from.
_NET30_INVOICE_TEMPLATE = "unico_sale_net30_approval.net30_invoice_email"


class AccountMove(models.Model):
    _inherit = "account.move"

    def _post(self, soft=True):
        """Mail Net 30 customer invoices as soon as they are validated.

        Hooked on ``_post`` rather than ``action_post`` so every route that
        validates an invoice is covered — the button, the autopost cron, and
        invoicing straight off a sale order.
        """
        posted = super()._post(soft=soft)
        posted._unico_send_net30_invoice_email()
        return posted

    def _unico_net30_invoices_to_send(self):
        """Posted customer invoices that should be mailed automatically.

        Credit notes are excluded: there is nothing for the customer to pay, so
        the portal pay link the mail is built around would be meaningless.
        ``is_move_sent`` doubles as the idempotency guard, so resetting an
        invoice to draft and re-posting it does not mail the customer twice.
        """
        return self.filtered(
            lambda move: move.move_type == "out_invoice"
            and move.state == "posted"
            and move.company_id.unico_auto_send_net30_invoices
            and move.partner_id.email
            and not move.is_move_sent
            and move.invoice_payment_term_id
            and move.invoice_payment_term_id._unico_is_net30()
        )

    def _unico_send_net30_invoice_email(self):
        """Send the Net 30 invoice mail, without ever endangering the posting.

        Rendering the template builds the invoice PDF, which can fail on its own
        (a broken report, a missing font). Posting an invoice is an accounting
        act and must not be rolled back because an email could not be built, so
        each send runs in its own savepoint and any failure is logged and left
        as a chatter note for someone to follow up by hand.
        """
        template = self.env.ref(_NET30_INVOICE_TEMPLATE, raise_if_not_found=False)
        moves = self._unico_net30_invoices_to_send()
        if moves and not template:
            _logger.warning(
                "Net 30 invoice email skipped: template %r not found.",
                _NET30_INVOICE_TEMPLATE)
            return False

        for move in moves:
            try:
                with self.env.cr.savepoint():
                    template.sudo().send_mail(move.id, force_send=True)
                    move.sudo().is_move_sent = True
            except Exception as exc:  # noqa: BLE001 - posting must not break
                _logger.warning(
                    "unico_sale_net30_approval: Net 30 invoice email failed for "
                    "%s: %s", move.name, exc)
                try:
                    move.sudo().message_post(body=_(
                        "The automatic Net 30 invoice email could not be sent. "
                        "The invoice is posted; please send it by hand with Send "
                        "&amp; Print. Error: %(error)s", error=str(exc)))
                except Exception:  # noqa: BLE001 - nor may the failure notice
                    _logger.exception(
                        "unico_sale_net30_approval: could not log the failed "
                        "Net 30 invoice email for move %s", move.id)
                continue
            move.sudo().message_post(body=_(
                "Invoice emailed automatically to %(customer)s: the payment term "
                "%(term)s is a Net 30 term.",
                customer=move.partner_id.email,
                term=move.invoice_payment_term_id.display_name))
        return True
