# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    multi_invoice_batch_key = fields.Char(
        string="Multi-Invoice Batch Key",
        readonly=True,
        copy=False,
        index=True,
        help="Internal idempotency key for a combined-invoice payment attempt.",
    )
    multi_invoice_link_issued_at = fields.Integer(
        string="Combined Link Issued At",
        readonly=True,
        copy=False,
        help="UTC Unix timestamp signed into the combined payment link.",
    )
    multi_invoice_refreshed_link = fields.Boolean(
        string="Paid From Refreshed Link",
        readonly=True,
        copy=False,
    )
    multi_invoice_collection_notified = fields.Boolean(
        string="AR Collection Notified",
        readonly=True,
        copy=False,
    )

    def _get_multi_invoice_base_invoices(self):
        """Return linked invoices without an optional surcharge invoice."""
        self.ensure_one()
        invoices = self.invoice_ids
        if "fee_move_id" in self._fields:
            invoices -= self.fee_move_id
        return invoices

    def _cancel_for_multi_invoice_balance_change(self):
        """Cancel this attempt only when no provider-side payment can still settle."""
        self.ensure_one()
        self.invalidate_recordset(["state", "provider_reference"])
        if self.state in ("cancel", "error"):
            return True
        if self.state not in ("draft", "pending", "authorized"):
            return False

        state_message = _(
            "Canceled because the balance on the combined invoices changed."
        )
        if not self.provider_reference:
            if self.state != "draft":
                return False
            self._set_canceled(state_message=state_message)
            return self.state == "cancel"
        if self.provider_code != "stripe":
            return False

        try:
            payment_intent = self._send_api_request(
                "POST",
                f"payment_intents/{self.provider_reference}/cancel",
            )
        except ValidationError:
            return False
        if payment_intent.get("status") != "canceled":
            return False
        self._set_canceled(state_message=state_message)
        return self.state == "cancel"

    def _post_process(self):
        result = super()._post_process()
        self.filtered(
            lambda tx: (
                tx.state == "done"
                and tx.multi_invoice_refreshed_link
                and not tx.multi_invoice_collection_notified
            )
        )._notify_refreshed_multi_invoice_collection()
        return result

    def _notify_refreshed_multi_invoice_collection(self):
        """Log and notify Accounting once a self-healed link is paid."""
        if not self:
            return
        accounting_group = self.env.ref(
            "account.group_account_invoice",
            raise_if_not_found=False,
        )
        accounting_users = (
            accounting_group.sudo().all_user_ids.filtered("active")
            if accounting_group
            else self.env["res.users"]
        )
        for tx in self:
            invoices = tx._get_multi_invoice_base_invoices()
            partner_ids = accounting_users.filtered(
                lambda user: tx.company_id in user.company_ids
            ).partner_id.ids
            body = _(
                "Payment %(reference)s for %(amount)s was received through a "
                "refreshed combined-invoice payment link.",
                reference=tx.reference,
                amount=tx.currency_id.format(tx.amount),
            )
            for invoice in invoices:
                invoice.message_post(
                    body=body,
                    partner_ids=partner_ids,
                    subtype_xmlid="mail.mt_note",
                )
            tx.multi_invoice_collection_notified = True

    @api.model
    def _cancel_stale_multi_invoice_attempts(
        self,
        batch_key,
        open_invoices,
        current_amount,
    ):
        """Cancel stale active attempts, but never replace a current active one."""
        active_txs = self.sudo().search([
            ("multi_invoice_batch_key", "=", batch_key),
            ("state", "in", ("draft", "pending", "authorized")),
        ], order="id")
        for tx in active_txs:
            base_invoices = tx._get_multi_invoice_base_invoices()
            is_current_attempt = (
                set(base_invoices.ids) == set(open_invoices.ids)
                and not tx.currency_id.compare_amounts(tx.amount, current_amount)
            )
            if is_current_attempt:
                return False
        for tx in active_txs:
            if not tx._cancel_for_multi_invoice_balance_change():
                return False
        return True
