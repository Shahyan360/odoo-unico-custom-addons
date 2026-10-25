# Part of Odoo. See LICENSE file for full copyright and licensing details.
import time
from math import isfinite

from werkzeug.exceptions import NotFound

from odoo import _
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.http import request, route
from odoo.tools.misc import formatLang

from odoo.addons.payment import utils as payment_utils
from odoo.addons.account_payment.controllers.payment import PaymentPortal
from odoo.addons.stripe_charge.controllers.portal import (
    _request_user_can_use_partner_tokens,
)


class MultiInvoicePaymentPortal(PaymentPortal):

    _MULTI_TX_ROUTE = "/payment/pay/multi_invoice/transaction"
    _MULTI_REFRESH_ROUTE = "/payment/pay/multi_invoice/refresh"
    _MULTI_STRIPE_REVIEW_ROUTE = (
        "/payment/pay/multi_invoice/stripe/surcharge/preview"
    )

    # ------------------------------------------------------------------
    # Payment form (GET)
    # ------------------------------------------------------------------
    @route(
        "/payment/pay/multi_invoice",
        type="http", methods=["GET"], auth="public", website=True, sitemap=False,
    )
    def multi_invoice_pay(self, invoice_ids=None, amount=None, access_token=None, **kwargs):
        """Render the standard payment form for an authenticated invoice batch."""
        try:
            invoices_sudo = self._get_multi_invoice_records(invoice_ids)
            signed_amount = self._cast_as_float(amount)
            if (
                signed_amount is None
                or not isfinite(signed_amount)
                or invoices_sudo.currency_id.compare_amounts(signed_amount, 0) <= 0
            ):
                raise NotFound()
            token_status = self._inspect_multi_invoice_access_token(
                access_token,
                invoices_sudo,
                signed_amount,
            )
            if not token_status["authenticated"]:
                raise NotFound()
            if token_status["expired"]:
                return self._render_expired_link(invoices_sudo)
            try:
                current_amount = invoices_sudo._get_multi_invoice_residual()
            except UserError:
                return self._render_balance_changed(
                    invoices_sudo,
                    signed_amount=signed_amount,
                    access_token=access_token,
                )
            if invoices_sudo.currency_id.compare_amounts(
                signed_amount, current_amount
            ):
                return self._render_balance_changed(
                    invoices_sudo,
                    signed_amount=signed_amount,
                    access_token=access_token,
                )
        except (UserError, ValidationError):
            raise NotFound() from None

        partner = invoices_sudo.partner_id
        currency = invoices_sudo.currency_id

        payment_access_token = payment_utils.generate_access_token(
            partner.id, current_amount, currency.id
        )
        kwargs.update({
            "reference": self._get_multi_invoice_reference(invoices_sudo),
            "currency_id": currency.id,
            "partner_id": partner.id,
            "company_id": invoices_sudo.company_id.id,
            "multi_invoice_ids": ",".join(str(i) for i in invoices_sudo.ids),
            "multi_invoice_access_token": access_token,
        })
        return super().payment_pay(
            amount=current_amount,
            access_token=payment_access_token,
            **kwargs,
        )

    def _get_extra_payment_form_values(
        self,
        multi_invoice_ids=None,
        multi_invoice_access_token=None,
        **kwargs,
    ):
        """Point the form at the multi-invoice transaction route when relevant."""
        form_values = super()._get_extra_payment_form_values(**kwargs)
        if multi_invoice_ids:
            invoices_sudo = self._get_multi_invoice_records(multi_invoice_ids)
            payment_summary = (
                invoices_sudo._get_public_invoice_payment_summary(
                    form_values.get("amount", kwargs.get("amount"))
                )
                if hasattr(
                    invoices_sudo,
                    "_get_public_invoice_payment_summary",
                )
                else {
                    "customer_name": invoices_sudo.partner_id.display_name,
                    "invoices": self._get_invoice_breakdown(invoices_sudo),
                }
            )
            extra_values = {
                "transaction_route": f"{self._MULTI_TX_ROUTE}/{multi_invoice_ids}",
                "landing_route": "/payment/confirmation",
                "access_token": multi_invoice_access_token,
                "payment_invoice_summary": payment_summary,
            }
            if hasattr(
                request.env["payment.provider"],
                "_stripe_build_card_payment_review",
            ):
                extra_values["stripe_review_route"] = (
                    f"{self._MULTI_STRIPE_REVIEW_ROUTE}/{multi_invoice_ids}"
                )
            form_values.update(extra_values)
            payment_partner = invoices_sudo.partner_id
            if len(payment_partner) > 1:
                payment_partner = payment_partner.commercial_partner_id
            if (
                len(payment_partner) == 1
                and len(invoices_sudo.company_id) == 1
                and len(invoices_sudo.currency_id) == 1
            ):
                company = invoices_sudo.company_id
                currency = invoices_sudo.currency_id
                current_amount = invoices_sudo._get_multi_invoice_residual()
                availability_report = {}
                providers_sudo = request.env[
                    "payment.provider"
                ].sudo()._get_compatible_providers(
                    company.id,
                    payment_partner.id,
                    current_amount,
                    currency_id=currency.id,
                    report=availability_report,
                )
                payment_methods_sudo = request.env[
                    "payment.method"
                ].sudo()._get_compatible_payment_methods(
                    providers_sudo.ids,
                    payment_partner.id,
                    currency_id=currency.id,
                    report=availability_report,
                )
                can_use_tokens = _request_user_can_use_partner_tokens(
                    payment_partner
                )
                tokens_sudo = request.env["payment.token"].sudo()
                if can_use_tokens:
                    tokens_sudo = tokens_sudo._get_available_tokens(
                        providers_sudo.ids,
                        payment_partner.id,
                    )
                show_tokenize_input_mapping = (
                    PaymentPortal._compute_show_tokenize_input_mapping(
                        providers_sudo
                    )
                    if can_use_tokens
                    else {provider.id: False for provider in providers_sudo}
                )
                form_values.update({
                    "partner_id": payment_partner.id,
                    "providers_sudo": providers_sudo,
                    "payment_methods_sudo": payment_methods_sudo,
                    "tokens_sudo": tokens_sudo,
                    "availability_report": availability_report,
                    "company_mismatch": (
                        not PaymentPortal._can_partner_pay_in_company(
                            payment_partner,
                            company,
                        )
                    ),
                    "show_tokenize_input_mapping": show_tokenize_input_mapping,
                })
        return form_values

    @route(
        f"{_MULTI_STRIPE_REVIEW_ROUTE}/<string:invoice_ids>",
        type="jsonrpc",
        auth="public",
    )
    def multi_invoice_stripe_surcharge_preview(
        self,
        invoice_ids,
        access_token,
        amount=None,
        provider_id=None,
        payment_method_id=None,
        token_id=None,
        flow=None,
        stripe_confirmation_token=None,
        **kwargs,
    ):
        """Return an authenticated exact review for a combined card payment."""
        invoices_sudo = self._get_multi_invoices(invoice_ids)
        current_amount = invoices_sudo._get_multi_invoice_residual()
        supplied_amount = self._cast_as_float(amount)
        token_status = self._inspect_multi_invoice_access_token(
            access_token,
            invoices_sudo,
            current_amount,
        )
        if (
            supplied_amount is None
            or not isfinite(supplied_amount)
            or invoices_sudo.currency_id.compare_amounts(
                supplied_amount,
                current_amount,
            )
            or not token_status["valid"]
        ):
            raise ValidationError(_(
                "The invoice balance changed. Refresh the payment page."
            ))

        invoices_sudo._check_no_unresolved_stripe_surcharge_accounting()

        if flow == "token" and not _request_user_can_use_partner_tokens(
            invoices_sudo.partner_id
        ):
            raise ValidationError(_(
                "Saved payment methods can only be used by the invoice customer."
            ))

        provider_id = self._cast_as_int(provider_id)
        provider_sudo = request.env[
            "payment.provider"
        ].sudo()._get_compatible_providers(
            invoices_sudo.company_id.id,
            invoices_sudo.partner_id.id,
            current_amount,
            currency_id=invoices_sudo.currency_id.id,
        ).filtered(
            lambda provider: provider.id == provider_id
            and provider.code == "stripe"
        )
        if not provider_sudo:
            raise ValidationError(_(
                "The selected payment provider is no longer available."
            ))
        option_values = {
            "flow": flow,
            "payment_method_id": payment_method_id,
            "token_id": token_id,
        }
        self._validate_payment_option(
            option_values,
            provider_sudo,
            invoices_sudo.partner_id,
            invoices_sudo.currency_id,
        )
        payment_method_sudo, payment_token_sudo = (
            self._get_review_payment_method(option_values)
        )
        return provider_sudo._stripe_build_card_payment_review(
            invoices=invoices_sudo,
            amount=current_amount,
            payment_method=payment_method_sudo,
            confirmation_token_id=stripe_confirmation_token,
            payment_token=payment_token_sudo,
        )

    # ------------------------------------------------------------------
    # Transaction creation (JSON-RPC, posted by the payment form)
    # ------------------------------------------------------------------
    @route(
        f"{_MULTI_TX_ROUTE}/<string:invoice_ids>",
        type="jsonrpc", auth="public",
    )
    def multi_invoice_transaction(self, invoice_ids, access_token, amount=None, **kwargs):
        """Create one transaction for a server-validated invoice batch."""
        invoices_sudo = self._get_multi_invoices(invoice_ids)
        current_amount = invoices_sudo._get_multi_invoice_residual()
        supplied_amount = self._cast_as_float(amount)
        token_status = self._inspect_multi_invoice_access_token(
            access_token,
            invoices_sudo,
            current_amount,
        )
        if (
            supplied_amount is None
            or not isfinite(supplied_amount)
            or invoices_sudo.currency_id.compare_amounts(supplied_amount, current_amount)
            or not token_status["valid"]
        ):
            raise ValidationError(_("The provided parameters are invalid."))

        self._validate_transaction_kwargs(
            kwargs,
            additional_allowed_keys=(
                "stripe_confirmation_token",
                "stripe_surcharge_review_token",
            ),
        )
        if kwargs.get("is_validation"):
            raise ValidationError(_("Invoice payment links cannot validate payment methods."))

        partner = invoices_sudo.partner_id
        currency = invoices_sudo.currency_id
        company = invoices_sudo.company_id
        provider_id = self._cast_as_int(kwargs.get("provider_id"))
        compatible_providers = request.env["payment.provider"].sudo()._get_compatible_providers(
            company.id,
            partner.id,
            current_amount,
            currency_id=currency.id,
        )
        provider_sudo = compatible_providers.filtered(lambda provider: provider.id == provider_id)
        if not provider_sudo:
            raise ValidationError(_("The selected payment provider is no longer available."))

        self._validate_payment_option(kwargs, provider_sudo, partner, currency)
        stripe_confirmation_token = kwargs.pop(
            "stripe_confirmation_token",
            None,
        )
        stripe_surcharge_review_token = kwargs.pop(
            "stripe_surcharge_review_token",
            None,
        )
        batch_key = invoices_sudo._get_multi_invoice_payment_batch_key()
        self._lock_multi_invoice_batch(batch_key)
        request.env.cr.execute(
            """
                SELECT id
                  FROM account_move
                 WHERE id = ANY(%s)
                 ORDER BY id
                 FOR UPDATE
            """,
            [invoices_sudo.ids],
        )

        # A second request can wait on the first one. Reload and authenticate the
        # amount again after acquiring the lock before creating or reusing a tx.
        invoices_sudo.invalidate_recordset([
            "state", "payment_state", "amount_residual",
        ])
        invoices_sudo._check_no_unapplied_stripe_recovery()
        invoices_sudo._check_no_unresolved_stripe_surcharge_accounting()
        current_amount = invoices_sudo._get_multi_invoice_residual()
        token_status = self._inspect_multi_invoice_access_token(
            access_token,
            invoices_sudo,
            current_amount,
        )
        if (
            invoices_sudo.currency_id.compare_amounts(supplied_amount, current_amount)
            or not token_status["valid"]
        ):
            raise ValidationError(_("The invoice balance changed. Refresh the payment page."))

        payment_method_sudo, payment_token_sudo = (
            self._get_review_payment_method(kwargs)
        )
        primary_method = (
            payment_method_sudo.primary_payment_method_id
            or payment_method_sudo
        )
        is_stripe_card = (
            hasattr(
                provider_sudo,
                "_stripe_validate_card_payment_review",
            )
            and provider_sudo.code == "stripe"
            and primary_method.code == "card"
            and kwargs.get("flow") in ("direct", "token")
        )
        if is_stripe_card:
            if kwargs.get("flow") == "direct" and not stripe_confirmation_token:
                raise ValidationError(_("Verify the card before reviewing payment."))
            if not stripe_surcharge_review_token:
                raise ValidationError(_(
                    "Review the current payment total before confirming payment."
                ))
        elif stripe_confirmation_token or stripe_surcharge_review_token:
            raise ValidationError(_(
                "Stripe card verification was supplied for an invalid payment flow."
            ))

        kwargs.update({
            "provider_id": provider_sudo.id,
            "amount": current_amount,
            "currency_id": currency.id,
            "partner_id": partner.id,
            "reference_prefix": False,
            "landing_route": "/payment/confirmation",
            "is_validation": False,
        })
        stripe_review = False
        if is_stripe_card:
            stripe_review = provider_sudo._stripe_validate_card_payment_review(
                review_token=stripe_surcharge_review_token,
                invoices=invoices_sudo,
                amount=current_amount,
                payment_method=payment_method_sudo,
                confirmation_token_id=stripe_confirmation_token,
                payment_token=(
                    payment_token_sudo
                    if kwargs.get("flow") == "token"
                    else False
                ),
            )
            if kwargs.get("flow") == "token":
                # Native payment charges saved cards while creating the
                # transaction. Reviewed Stripe payments must first commit the
                # local transaction and are confirmed by the second request.
                request.update_context(delay_token_charge=True)
        tx_sudo = self._get_reusable_multi_invoice_transaction(
            batch_key,
            invoices_sudo,
            kwargs,
            token_status=token_status,
            stripe_confirmation_token_id=stripe_confirmation_token or False,
        )
        if not tx_sudo:
            custom_create_values = {
                "invoice_ids": [Command.set(invoices_sudo.ids)],
                "multi_invoice_batch_key": batch_key,
                "multi_invoice_link_issued_at": token_status["issued_at"],
                "multi_invoice_refreshed_link": token_status["refreshed"],
            }
            if stripe_review:
                custom_create_values.update({
                    "stripe_confirmation_token_id": (
                        stripe_confirmation_token or False
                    ),
                    "stripe_card_funding": stripe_review["funding"],
                    "stripe_card_brand": stripe_review["brand"],
                    "stripe_card_last4": stripe_review["last4"],
                    "stripe_surcharge_net_amount": stripe_review["price_unit"],
                    "stripe_reviewed_surcharge_amount": (
                        stripe_review["surcharge_amount"]
                    ),
                    "stripe_surcharge_reviewed": True,
                    "stripe_reviewed_document_balance": current_amount,
                })
                # Keep the reviewed payment inputs in the controller override
                # chain as well. stripe_charge revalidates them immediately
                # before ORM creation, while the values above make this route's
                # transaction contract explicit and independently testable.
                kwargs.update({
                    "stripe_confirmation_token": stripe_confirmation_token,
                    "stripe_surcharge_review_token": (
                        stripe_surcharge_review_token
                    ),
                })
            tx_sudo = self._create_transaction(
                custom_create_values=custom_create_values,
                **kwargs,
            )
            confirmation_token = payment_utils.generate_access_token(
                tx_sudo.partner_id.id, tx_sudo.amount, tx_sudo.currency_id.id
            )
            self._update_landing_route(tx_sudo, confirmation_token)
        return tx_sudo._get_processing_values()

    @route(
        _MULTI_REFRESH_ROUTE,
        type="http",
        methods=["POST"],
        auth="public",
        website=True,
        sitemap=False,
        csrf=False,
    )
    def multi_invoice_refresh(
        self,
        invoice_ids=None,
        amount=None,
        access_token=None,
        **kwargs,
    ):
        """Close a stale attempt and redirect to the currently payable balance."""
        try:
            invoices_sudo = self._get_multi_invoice_records(invoice_ids)
            signed_amount = self._cast_as_float(amount)
            token_status = self._inspect_multi_invoice_access_token(
                access_token,
                invoices_sudo,
                signed_amount,
            )
            if (
                signed_amount is None
                or not isfinite(signed_amount)
                or invoices_sudo.currency_id.compare_amounts(signed_amount, 0) <= 0
                or not token_status["valid"]
            ):
                raise NotFound()
        except (UserError, ValidationError):
            raise NotFound() from None

        batch_key = invoices_sudo._get_multi_invoice_payment_batch_key()
        self._lock_multi_invoice_batch(batch_key)
        invoices_sudo.invalidate_recordset([
            "state", "payment_state", "amount_residual",
        ])
        open_invoices, current_amount = (
            invoices_sudo._get_open_multi_invoice_residual()
        )
        if not request.env[
            "payment.transaction"
        ].sudo()._cancel_stale_multi_invoice_attempts(
            batch_key,
            open_invoices,
            current_amount,
        ):
            return self._render_balance_changed(
                invoices_sudo,
                signed_amount=signed_amount,
                access_token=access_token,
                refresh_error=_(
                    "A payment for this balance is already processing or could "
                    "not be safely canceled. Contact Accounts Receivable before "
                    "trying another payment."
                ),
            )

        if len(open_invoices) >= 2:
            return request.redirect(
                open_invoices._get_multi_invoice_payment_url(refreshed=True),
                code=303,
            )
        if len(open_invoices) == 1:
            return request.redirect(open_invoices.get_portal_url(), code=303)
        return self._render_balance_changed(
            invoices_sudo,
            signed_amount=signed_amount,
            access_token=access_token,
            refresh_complete=True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_multi_invoice_records(invoice_ids):
        """Parse the CSV id list and return an authenticated batch candidate."""
        try:
            parts = (invoice_ids or "").split(",")
            ids = [int(part) for part in parts]
        except (AttributeError, TypeError, ValueError):
            raise ValidationError(_("The provided parameters are invalid."))
        if (
            len(ids) < 2
            or len(ids) != len(set(ids))
            or any(invoice_id <= 0 for invoice_id in ids)
        ):
            raise ValidationError(_("The provided parameters are invalid."))

        invoices_sudo = request.env["account.move"].sudo().browse(sorted(ids)).exists()
        if len(invoices_sudo) != len(ids):
            raise ValidationError(_("The provided parameters are invalid."))
        try:
            invoices_sudo._check_multi_invoice_batch_identity()
        except UserError:
            raise ValidationError(_("The provided parameters are invalid."))
        return invoices_sudo

    @staticmethod
    def _get_multi_invoices(invoice_ids):
        """Return a consistent batch that is currently payable."""
        invoices_sudo = MultiInvoicePaymentPortal._get_multi_invoice_records(invoice_ids)
        try:
            invoices_sudo._check_multi_invoice_payment_consistency()
        except UserError:
            raise ValidationError(_("The provided parameters are invalid."))
        return invoices_sudo

    @staticmethod
    def _inspect_multi_invoice_access_token(access_token, invoices_sudo, amount):
        """Authenticate the versioned token and report its bounded lifetime."""
        result = {
            "authenticated": False,
            "expired": False,
            "valid": False,
            "issued_at": 0,
            "refreshed": False,
        }
        if "." not in (access_token or ""):
            legacy_values = (
                invoices_sudo._get_legacy_multi_invoice_payment_token_values(
                    amount
                )
            )
            if payment_utils.check_access_token(
                access_token,
                *legacy_values,
            ):
                result.update({
                    "authenticated": True,
                    "expired": True,
                })
            return result
        try:
            version, issued_at, refreshed, signature = (access_token or "").split(".")
            issued_at = int(issued_at)
            refreshed = bool(int(refreshed))
        except (AttributeError, TypeError, ValueError):
            return result
        if version != invoices_sudo._MULTI_INVOICE_TOKEN_VERSION:
            return result
        token_values = invoices_sudo._get_multi_invoice_payment_token_values(
            amount,
            issued_at=issued_at,
            refreshed=refreshed,
        )
        if not payment_utils.check_access_token(signature, *token_values):
            return result

        now = int(time.time())
        validity = invoices_sudo._get_multi_invoice_payment_link_validity_seconds()
        expired = issued_at > now + 300 or now - issued_at > validity
        result.update({
            "authenticated": True,
            "expired": expired,
            "valid": not expired,
            "issued_at": issued_at,
            "refreshed": refreshed,
        })
        return result

    @staticmethod
    def _check_multi_invoice_access_token(access_token, invoices_sudo, amount):
        return MultiInvoicePaymentPortal._inspect_multi_invoice_access_token(
            access_token,
            invoices_sudo,
            amount,
        )["valid"]

    @staticmethod
    def _lock_multi_invoice_batch(batch_key):
        """Serialize transaction creation for this exact invoice batch."""
        lock_id = int.from_bytes(
            bytes.fromhex(batch_key[:16]), byteorder="big", signed=True
        )
        request.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])

    @staticmethod
    def _get_reusable_multi_invoice_transaction(
        batch_key,
        invoices_sudo,
        kwargs,
        *,
        token_status,
        stripe_confirmation_token_id=False,
    ):
        """Reuse an identical draft attempt or block an active different attempt."""
        active_txs = request.env["payment.transaction"].sudo().search([
            ("multi_invoice_batch_key", "=", batch_key),
            ("state", "in", ("draft", "pending", "authorized")),
        ], order="id desc")

        flow = kwargs.get("flow")
        token_id = kwargs.get("token_id") or False
        payment_method_id = kwargs.get("payment_method_id") or False
        if flow == "token":
            token_sudo = request.env["payment.token"].sudo().browse(token_id)
            payment_method_id = token_sudo.payment_method_id.id
        expected_operation = f"online_{flow}"

        matching_attempts = active_txs.filtered(
            lambda tx: (
                tx.state in ("draft", "pending")
                and tx.provider_id.id == kwargs["provider_id"]
                and tx.payment_method_id.id == payment_method_id
                and tx.token_id.id == token_id
                and tx.operation == expected_operation
                and tx.partner_id == invoices_sudo.partner_id
                and tx.currency_id == invoices_sudo.currency_id
                and set(tx._get_multi_invoice_base_invoices().ids)
                == set(invoices_sudo.ids)
                and tx.multi_invoice_link_issued_at
                == token_status["issued_at"]
                and tx.multi_invoice_refreshed_link == token_status["refreshed"]
                and (
                    flow == "token"
                    or tx.stripe_confirmation_token_id
                    == stripe_confirmation_token_id
                )
                and (
                    not (
                        tx.provider_code == "stripe"
                        and tx.primary_payment_method_id.code == "card"
                    )
                    or tx.stripe_surcharge_reviewed
                )
                and not tx.currency_id.compare_amounts(
                    tx.amount, kwargs["amount"]
                )
            )
        )
        reviewed_stripe_txs = active_txs.filtered(
            lambda tx: tx._is_reviewed_stripe_card_payment()
        )
        if active_txs and reviewed_stripe_txs == active_txs:
            return request.env[
                "payment.transaction"
            ].sudo()._stripe_reuse_or_replace_reviewed_attempt(
                active_txs=active_txs,
                matching_txs=matching_attempts,
                replacement_message=_(
                    "Canceled because a newer payment attempt was started for "
                    "these invoices."
                ),
                blocking_message=_(
                    "A payment for these invoices is already in progress. "
                    "Wait for it to finish or contact Accounts Receivable."
                ),
            )
        if len(active_txs) == 1 and matching_attempts:
            return matching_attempts
        if active_txs:
            raise ValidationError(_(
                "A payment for these invoices is already in progress. "
                "Wait for it to finish or contact Accounts Receivable."
            ))

        completed_txs = request.env["payment.transaction"].sudo().search([
            ("multi_invoice_batch_key", "=", batch_key),
            ("state", "=", "done"),
        ])
        same_completed_amount = completed_txs.filtered(
            lambda tx: (
                set(tx._get_multi_invoice_base_invoices().ids)
                == set(invoices_sudo.ids)
                and not tx.currency_id.compare_amounts(
                    tx.amount, kwargs["amount"]
                )
            )
        )
        if same_completed_amount:
            raise ValidationError(_(
                "A payment for this balance has already completed. "
                "Refresh the page before trying again."
            ))
        return request.env["payment.transaction"].sudo()

    @staticmethod
    def _get_invoice_breakdown(invoices_sudo):
        invoices_sudo.invalidate_recordset([
            "state",
            "payment_state",
            "amount_total",
            "amount_residual",
            "invoice_date",
            "invoice_date_due",
        ])
        return [{
            "name": invoice.name,
            "invoice_date_formatted": (
                str(invoice.invoice_date) if invoice.invoice_date else "-"
            ),
            "invoice_date_due": invoice.invoice_date_due,
            "amount_total": formatLang(
                request.env,
                invoice.amount_total,
                currency_obj=invoice.currency_id,
            ),
            "amount_residual": formatLang(
                request.env,
                invoice.amount_residual,
                currency_obj=invoice.currency_id,
            ),
            "is_open": (
                invoice.state == "posted"
                and invoice.payment_state in ("not_paid", "partial")
                and invoice.currency_id.compare_amounts(
                    invoice.amount_residual,
                    0,
                ) > 0
            ),
        } for invoice in invoices_sudo.sorted("invoice_date_due")]

    @staticmethod
    def _render_expired_link(invoices_sudo):
        return request.render(
            "unico_payment_multiple_invoices.payment_link_expired",
            {
                "invoice_breakdown": (
                    MultiInvoicePaymentPortal._get_invoice_breakdown(invoices_sudo)
                ),
            },
        )

    @staticmethod
    def _render_balance_changed(
        invoices_sudo,
        *,
        signed_amount,
        access_token,
        refresh_error=False,
        refresh_complete=False,
    ):
        invoices_sudo.invalidate_recordset([
            "state", "payment_state", "amount_residual",
        ])
        open_invoices, current_amount = (
            invoices_sudo._get_open_multi_invoice_residual()
        )
        formatted_current_amount = formatLang(
            request.env,
            current_amount,
            currency_obj=invoices_sudo.currency_id,
        )
        return request.render(
            "unico_payment_multiple_invoices.payment_link_balance_changed",
            {
                "refresh_route": MultiInvoicePaymentPortal._MULTI_REFRESH_ROUTE,
                "invoice_ids": ",".join(str(i) for i in invoices_sudo.ids),
                "signed_amount": repr(signed_amount),
                "access_token": access_token,
                "formatted_current_amount": formatted_current_amount,
                "remaining_invoice_count": len(open_invoices),
                "invoice_breakdown": (
                    MultiInvoicePaymentPortal._get_invoice_breakdown(invoices_sudo)
                ),
                "refresh_error": refresh_error,
                "refresh_complete": refresh_complete,
            },
        )

    @staticmethod
    def _validate_payment_option(kwargs, provider_sudo, partner, currency):
        """Recheck the browser-selected method or token against the signed batch."""
        flow = kwargs.get("flow")
        if flow in ("direct", "redirect"):
            payment_method_id = MultiInvoicePaymentPortal._cast_as_int(
                kwargs.get("payment_method_id")
            )
            payment_methods_sudo = request.env[
                "payment.method"
            ].sudo()._get_compatible_payment_methods(
                provider_sudo.ids,
                partner.id,
                currency_id=currency.id,
            )
            if payment_method_id not in payment_methods_sudo.ids:
                raise ValidationError(_("The selected payment method is no longer available."))
            kwargs["payment_method_id"] = payment_method_id
            kwargs["token_id"] = None
        elif flow == "token":
            token_id = MultiInvoicePaymentPortal._cast_as_int(kwargs.get("token_id"))
            token_sudo = request.env["payment.token"].sudo().browse(token_id).exists()
            if (
                not token_sudo
                or not token_sudo.active
                or token_sudo.provider_id != provider_sudo
                or token_sudo.partner_id.commercial_partner_id
                != partner.commercial_partner_id
            ):
                raise ValidationError(_("The selected payment token is invalid."))
            kwargs["token_id"] = token_sudo.id
            kwargs["payment_method_id"] = None
        else:
            raise ValidationError(_("The selected payment flow is invalid."))

    @staticmethod
    def _get_review_payment_method(kwargs):
        if kwargs.get("flow") == "token":
            payment_token_sudo = request.env["payment.token"].sudo().browse(
                kwargs.get("token_id")
            )
            return payment_token_sudo.payment_method_id, payment_token_sudo
        payment_method_sudo = request.env["payment.method"].sudo().browse(
            kwargs.get("payment_method_id")
        )
        return payment_method_sudo, request.env["payment.token"].sudo()

    @staticmethod
    def _get_multi_invoice_reference(invoices_sudo):
        names = invoices_sudo.mapped("name")
        return ", ".join(names) if len(names) <= 3 else _("%s invoices", len(names))
