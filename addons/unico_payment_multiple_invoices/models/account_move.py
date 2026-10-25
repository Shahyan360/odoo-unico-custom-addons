# Part of Odoo. See LICENSE file for full copyright and licensing details.
import hashlib
import time
from urllib.parse import urlencode

from odoo import _, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from odoo.tools.misc import hmac as hmac_tool


class AccountMove(models.Model):
    _inherit = "account.move"

    _MULTI_INVOICE_TOKEN_VERSION = "v2"
    _MULTI_INVOICE_LINK_VALIDITY_DAYS = 90

    # -- Public entry point (called by the server action) --------------------
    def action_generate_multi_invoice_payment_link(self):
        """Open a wizard exposing a single payment link for ``self``.

        Validates that every selected move is an open customer invoice for the
        same partner/currency/company, then returns a window action pointing at
        the ``unico.multi.invoice.payment.link`` wizard.
        """
        self._check_multi_invoice_payment_consistency()

        amount = self._get_multi_invoice_residual()
        wizard = self.env["unico.multi.invoice.payment.link"].create({
            "invoice_ids": [(6, 0, self.ids)],
            "partner_id": self.partner_id.id,
            "currency_id": self.currency_id.id,
            "amount": amount,
            "payment_link": self._get_multi_invoice_payment_url(),
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Payment Link — %s invoices", len(self)),
            "res_model": "unico.multi.invoice.payment.link",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    # -- Helpers -------------------------------------------------------------
    def _check_multi_invoice_batch_identity(self):
        """Validate the immutable properties used to authenticate a batch."""
        if len(self) < 2:
            raise UserError(_(
                "Select at least two invoices to generate a combined payment "
                "link. Use the standard button for a single invoice."
            ))
        if any(m.move_type != "out_invoice" for m in self):
            raise UserError(_(
                "Only customer invoices can be paid with a combined link. "
                "Apply credit notes to the invoices before generating the link."
            ))
        if len(self.partner_id) != 1 or not self.partner_id:
            raise UserError(_("All selected invoices must belong to the same customer."))
        if len(self.currency_id) != 1:
            raise UserError(_("All selected invoices must share the same currency."))
        if len(self.company_id) != 1:
            raise UserError(_("All selected invoices must belong to the same company."))

    def _check_multi_invoice_payment_consistency(self):
        """Raise ``UserError`` if ``self`` cannot currently be paid with one link."""
        self._check_multi_invoice_batch_identity()
        if any(m.state != "posted" for m in self):
            raise UserError(_(
                "Only posted invoices can be paid. Please remove drafts/cancelled entries."
            ))
        if any(m.payment_state not in ("not_paid", "partial") for m in self):
            raise UserError(_(
                "Every selected invoice must have an open balance "
                "(not paid or partially paid)."
            ))

    def _get_multi_invoice_residual(self):
        """Return the current positive residual of the validated invoice batch."""
        self._check_multi_invoice_payment_consistency()
        total = float_round(
            sum(self.mapped("amount_residual")),
            precision_rounding=self.currency_id.rounding,
        )
        if self.currency_id.compare_amounts(total, 0.0) <= 0:
            raise UserError(_("The combined invoice balance must be greater than zero."))
        return total

    def _get_open_multi_invoice_members(self):
        """Return the original batch members that still have a positive balance."""
        self._check_multi_invoice_batch_identity()
        return self.filtered(
            lambda move: (
                move.state == "posted"
                and move.payment_state in ("not_paid", "partial")
                and move.currency_id.compare_amounts(move.amount_residual, 0) > 0
            )
        )

    def _get_open_multi_invoice_residual(self):
        """Return the currently open batch members and their rounded residual."""
        open_invoices = self._get_open_multi_invoice_members()
        total = float_round(
            sum(open_invoices.mapped("amount_residual")),
            precision_rounding=self.currency_id.rounding,
        )
        return open_invoices, total

    def _get_multi_invoice_payment_link_validity_seconds(self):
        """Return the configured positive lifetime for combined payment links."""
        value = self.env["ir.config_parameter"].sudo().get_param(
            "unico_payment_multiple_invoices.link_validity_days",
            str(self._MULTI_INVOICE_LINK_VALIDITY_DAYS),
        )
        try:
            days = int(value)
        except (TypeError, ValueError):
            days = self._MULTI_INVOICE_LINK_VALIDITY_DAYS
        if days <= 0:
            days = self._MULTI_INVOICE_LINK_VALIDITY_DAYS
        return days * 24 * 60 * 60

    def _get_multi_invoice_payment_token_values(
        self,
        amount=None,
        *,
        issued_at,
        refreshed=False,
    ):
        """Return the canonical values signed into a combined payment link."""
        self._check_multi_invoice_batch_identity()
        amount = self._get_multi_invoice_residual() if amount is None else amount
        canonical_invoice_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.ids)
        )
        return (
            self.partner_id.id,
            amount,
            self.currency_id.id,
            self.company_id.id,
            canonical_invoice_ids,
            self._MULTI_INVOICE_TOKEN_VERSION,
            int(issued_at),
            int(bool(refreshed)),
        )

    def _get_legacy_multi_invoice_payment_token_values(self, amount):
        """Return the values used by links generated before bounded expiry."""
        self._check_multi_invoice_batch_identity()
        canonical_invoice_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.ids)
        )
        return (
            self.partner_id.id,
            amount,
            self.currency_id.id,
            self.company_id.id,
            canonical_invoice_ids,
        )

    def _get_multi_invoice_payment_access_token(
        self,
        amount=None,
        *,
        issued_at=None,
        refreshed=False,
    ):
        """Generate a request-independent HMAC for this exact invoice batch."""
        issued_at = int(time.time()) if issued_at is None else int(issued_at)
        token_str = "|".join(
            str(value)
            for value in self._get_multi_invoice_payment_token_values(
                amount,
                issued_at=issued_at,
                refreshed=refreshed,
            )
        )
        signature = hmac_tool(
            self.env(su=True),
            "generate_access_token",
            token_str,
        )
        return ".".join((
            self._MULTI_INVOICE_TOKEN_VERSION,
            str(issued_at),
            str(int(bool(refreshed))),
            signature,
        ))

    def _get_multi_invoice_payment_batch_key(self):
        """Return a bounded identifier used to serialize payment attempts."""
        self._check_multi_invoice_batch_identity()
        canonical_invoice_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.ids)
        )
        payload = "|".join((
            str(self.company_id.id),
            str(self.partner_id.commercial_partner_id.id),
            str(self.currency_id.id),
            canonical_invoice_ids,
        ))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _get_multi_invoice_payment_url(self, *, refreshed=False):
        """Build the signed ``/payment/pay`` URL covering all of ``self``."""
        self._check_multi_invoice_payment_consistency()
        amount = self._get_multi_invoice_residual()
        issued_at = int(time.time())
        canonical_invoice_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.ids)
        )
        params = {
            "invoice_ids": canonical_invoice_ids,
            "amount": repr(amount),
            "access_token": self._get_multi_invoice_payment_access_token(
                amount,
                issued_at=issued_at,
                refreshed=refreshed,
            ),
        }
        base_url = self[:1].get_base_url()
        return f"{base_url}/payment/pay/multi_invoice?{urlencode(params)}"
