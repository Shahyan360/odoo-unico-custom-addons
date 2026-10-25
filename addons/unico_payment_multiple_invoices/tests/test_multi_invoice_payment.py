# Part of Odoo. See LICENSE file for full copyright and licensing details.
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from lxml import html

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import JsonRpcException, tagged

from odoo.addons.account_payment.tests.common import AccountPaymentCommon
from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.tests.http_common import PaymentHttpCommon


@tagged("post_install", "-at_install")
class TestMultiInvoicePayment(AccountPaymentCommon, PaymentHttpCommon):

    def setUp(self):
        super().setUp()
        self.invoice_1 = self.init_invoice(
            "out_invoice",
            self.partner,
            amounts=[100.0],
            currency=self.currency,
        )
        self.invoice_2 = self.init_invoice(
            "out_invoice",
            self.partner,
            amounts=[60.0],
            currency=self.currency,
        )
        (self.invoice_1 | self.invoice_2).action_post()
        self.invoices = self.invoice_1 | self.invoice_2

    def _get_link_parts(self, invoices=None):
        invoices = invoices or self.invoices
        parsed_url = urlsplit(invoices._get_multi_invoice_payment_url())
        params = {
            key: values[0]
            for key, values in parse_qs(parsed_url.query).items()
        }
        return parsed_url.path, params

    def _get_payment_form_context(self):
        path, params = self._get_link_parts()
        response = self._make_http_get_request(self._build_url(path), params)
        self.assertEqual(response.status_code, 200)
        return self._get_payment_context(response)

    def _prepare_transaction_values(self, payment_context):
        return {
            "provider_id": self.provider.id,
            "payment_method_id": self.payment_method_id,
            "token_id": None,
            "amount": payment_context["amount"],
            "flow": "direct",
            "tokenization_requested": False,
            "landing_route": payment_context["landing_route"],
            "access_token": payment_context["access_token"],
            "is_validation": False,
        }

    def _get_balance_refresh_form(self, response):
        document = html.fromstring(response.content)
        form = document.xpath(
            "//form[@action='/payment/pay/multi_invoice/refresh']"
        )[0]
        values = {
            field.get("name"): field.get("value")
            for field in form.xpath(".//input[@name]")
        }
        return form.get("action"), values

    def _submit_balance_refresh(self, response):
        action, values = self._get_balance_refresh_form(response)
        return self.url_open(
            self._build_url(action),
            data=values,
            method="POST",
            allow_redirects=False,
        )

    def _configure_stripe_surcharge_provider(self, test_key):
        stripe_provider = self.env["payment.provider"].search([
            ("code", "=", "stripe"),
            ("company_id", "=", self.company.id),
        ], limit=1)
        if not stripe_provider:
            self.skipTest("No Stripe provider exists for the test company.")

        card_method = self.env.ref("payment.payment_method_card")
        surcharge_product = self.env["product.product"].create({
            "name": f"Stripe Processing Surcharge - {test_key}",
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
            "company_id": self.company.id,
            "property_account_income_id": self.company_data[
                "default_account_revenue"
            ].id,
            "taxes_id": [Command.clear()],
        })
        stripe_provider.write({
            "state": "test",
            "is_published": True,
            "stripe_publishable_key": f"pk_test_{test_key}",
            "stripe_secret_key": f"sk_test_{test_key}",
            "fees_active": True,
            "fees_dom_fixed": 0.0,
            "fees_dom_var": 0.03,
            "fees_int_fixed": 0.0,
            "fees_int_var": 0.03,
            "stripe_surcharge_product_id": surcharge_product.id,
            "payment_method_ids": [Command.link(card_method.id)],
            "available_currency_ids": [Command.link(self.currency.id)],
        })
        return stripe_provider, card_method

    def test_link_is_signed_for_exact_invoice_batch(self):
        path, params = self._get_link_parts()
        canonical_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.invoices.ids)
        )
        amount = self.invoices._get_multi_invoice_residual()
        version, issued_at, refreshed, signature = params["access_token"].split(".")
        expected_signature = self._generate_test_access_token(
            self.partner.id,
            amount,
            self.currency.id,
            self.company.id,
            canonical_ids,
            version,
            int(issued_at),
            int(refreshed),
        )

        self.assertEqual(path, "/payment/pay/multi_invoice")
        self.assertEqual(params["invoice_ids"], canonical_ids)
        self.assertEqual(float(params["amount"]), amount)
        self.assertEqual(version, "v2")
        self.assertEqual(refreshed, "0")
        self.assertEqual(signature, expected_signature)

    def test_credit_notes_are_not_supported(self):
        credit_note = self.init_invoice(
            "out_refund",
            self.partner,
            amounts=[20.0],
            currency=self.currency,
        )
        credit_note.action_post()

        with self.assertRaisesRegex(UserError, "Only customer invoices"):
            (self.invoice_1 | credit_note)._check_multi_invoice_payment_consistency()

    def test_payment_form_uses_server_amount_and_public_confirmation(self):
        payment_context = self._get_payment_form_context()

        self.assertEqual(
            payment_context["amount"],
            self.invoices._get_multi_invoice_residual(),
        )
        self.assertEqual(
            payment_context["transaction_route"],
            "/payment/pay/multi_invoice/transaction/"
            + ",".join(str(invoice_id) for invoice_id in sorted(self.invoices.ids)),
        )
        self.assertEqual(payment_context["landing_route"], "/payment/confirmation")
        self.assertTrue(
            self.env["account.move"]._get_multi_invoice_payment_link_validity_seconds()
            > 0
        )
        if "fees_active" in self.env["payment.provider"]._fields:
            self.assertEqual(
                payment_context["stripe_review_route"],
                "/payment/pay/multi_invoice/stripe/surcharge/preview/"
                + ",".join(
                    str(invoice_id)
                    for invoice_id in sorted(self.invoices.ids)
                ),
            )

    def test_logged_out_payment_page_shows_every_invoice_detail(self):
        path, params = self._get_link_parts()
        response = self._make_http_get_request(self._build_url(path), params)

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.partner.display_name.encode(), response.content)
        self.assertIn(self.invoice_1.name.encode(), response.content)
        self.assertIn(self.invoice_2.name.encode(), response.content)
        self.assertIn(b"Invoice date", response.content)
        self.assertIn(b"Due date", response.content)
        self.assertIn(b"Original total", response.content)
        self.assertIn(b"Open balance", response.content)

    def test_payment_form_rejects_invalid_link_token(self):
        path, params = self._get_link_parts()
        params["access_token"] = "invalid"

        response = self._make_http_get_request(self._build_url(path), params)

        self.assertEqual(response.status_code, 404)

    def test_balance_refresh_rejects_invalid_link_token(self):
        _path, params = self._get_link_parts()
        params["access_token"] = "invalid"

        response = self.url_open(
            self._build_url("/payment/pay/multi_invoice/refresh"),
            data=params,
            method="POST",
            allow_redirects=False,
        )

        self.assertEqual(response.status_code, 404)

    def test_stale_link_shows_updated_balance_and_refreshes(self):
        path, params = self._get_link_parts()
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])

        response = self._make_http_get_request(self._build_url(path), params)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your balance has changed", response.content)
        self.assertIn(b"Continue with current balance", response.content)
        self.assertIn(self.invoice_1.name.encode(), response.content)
        self.assertIn(self.invoice_2.name.encode(), response.content)
        refresh_response = self._submit_balance_refresh(response)
        self.assertEqual(refresh_response.status_code, 303)
        refreshed_url = urlsplit(refresh_response.headers["Location"])
        refreshed_params = {
            key: values[0]
            for key, values in parse_qs(refreshed_url.query).items()
        }
        refreshed_response = self._make_http_get_request(
            self._build_url(refreshed_url.path),
            refreshed_params,
        )
        self.assertEqual(refreshed_response.status_code, 200)
        self.assertEqual(
            self._get_payment_context(refreshed_response)["amount"],
            self.invoices._get_multi_invoice_residual(),
        )
        self.assertEqual(refreshed_params["access_token"].split(".")[2], "1")

    def test_expired_link_shows_friendly_page_with_invoice_detail(self):
        path, params = self._get_link_parts()
        issued_at = int(params["access_token"].split(".")[1])
        validity = self.invoices._get_multi_invoice_payment_link_validity_seconds()

        with patch(
            "odoo.addons.unico_payment_multiple_invoices.controllers.portal.time.time",
            return_value=issued_at + validity + 1,
        ):
            response = self._make_http_get_request(
                self._build_url(path),
                params,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This payment link has expired", response.content)
        self.assertIn(self.invoice_1.name.encode(), response.content)
        self.assertIn(self.invoice_2.name.encode(), response.content)
        self.assertNotIn(b"Continue with current balance", response.content)

    def test_legacy_signed_link_shows_friendly_expired_page(self):
        path, params = self._get_link_parts()
        canonical_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.invoices.ids)
        )
        params["access_token"] = self._generate_test_access_token(
            self.partner.id,
            self.invoices._get_multi_invoice_residual(),
            self.currency.id,
            self.company.id,
            canonical_ids,
        )

        response = self._make_http_get_request(self._build_url(path), params)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This payment link has expired", response.content)
        self.assertIn(self.invoice_1.name.encode(), response.content)
        self.assertIn(self.invoice_2.name.encode(), response.content)

    def test_refreshed_link_payment_notifies_accounting_once(self):
        path, params = self._get_link_parts()
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])
        stale_response = self._make_http_get_request(
            self._build_url(path),
            params,
        )
        refresh_response = self._submit_balance_refresh(stale_response)
        refreshed_url = urlsplit(refresh_response.headers["Location"])
        refreshed_params = {
            key: values[0]
            for key, values in parse_qs(refreshed_url.query).items()
        }
        payment_response = self._make_http_get_request(
            self._build_url(refreshed_url.path),
            refreshed_params,
        )
        payment_context = self._get_payment_context(payment_response)
        processing_values = self.make_jsonrpc_request(
            self._build_url(payment_context["transaction_route"]),
            self._prepare_transaction_values(payment_context),
        )
        tx_sudo = self._get_tx(processing_values["reference"])
        message_count = len(self.invoice_1.message_ids)

        tx_sudo._set_done()
        tx_sudo._post_process()
        tx_sudo._post_process()

        self.assertTrue(tx_sudo.multi_invoice_refreshed_link)
        self.assertTrue(tx_sudo.multi_invoice_collection_notified)
        self.assertGreater(len(self.invoice_1.message_ids), message_count)
        notes = self.invoice_1.message_ids.filtered(
            lambda message: "refreshed combined-invoice" in (message.body or "")
        )
        self.assertEqual(len(notes), 1)

    def test_stale_link_opens_single_remaining_invoice(self):
        path, params = self._get_link_parts()
        self._register_payment(
            self.invoice_1,
            amount=self.invoice_1.amount_residual,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])

        response = self._make_http_get_request(self._build_url(path), params)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Open remaining invoice", response.content)
        refresh_response = self._submit_balance_refresh(response)
        self.assertEqual(refresh_response.status_code, 303)
        location = refresh_response.headers["Location"]
        self.assertIn(f"/my/invoices/{self.invoice_2.id}", location)
        self.assertIn("access_token=", location)

    def test_balance_refresh_cancels_stale_local_draft(self):
        path, params = self._get_link_parts()
        payment_context = self._get_payment_form_context()
        processing_values = self.make_jsonrpc_request(
            self._build_url(payment_context["transaction_route"]),
            self._prepare_transaction_values(payment_context),
        )
        tx_sudo = self._get_tx(processing_values["reference"])
        self.assertEqual(tx_sudo.state, "draft")
        self.assertFalse(tx_sudo.provider_reference)
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])

        response = self._make_http_get_request(self._build_url(path), params)
        refresh_response = self._submit_balance_refresh(response)

        self.assertEqual(refresh_response.status_code, 303)
        tx_sudo.invalidate_recordset(["state"])
        self.assertEqual(tx_sudo.state, "cancel")

    def test_balance_refresh_cancels_stale_stripe_payment_intent(self):
        path, params = self._get_link_parts()
        stripe_provider = self.env["payment.provider"].search([
            ("code", "=", "stripe"),
            ("company_id", "=", self.company.id),
        ], limit=1)
        if (
            not stripe_provider
            or "stripe_publishable_key" not in stripe_provider._fields
        ):
            self.skipTest("The Stripe payment provider is not installed.")
        card_method = self.env.ref("payment.payment_method_card")
        provider_values = {
            "state": "test",
            "stripe_publishable_key": "pk_test_stale_balance",
            "stripe_secret_key": "sk_test_stale_balance",
            "payment_method_ids": [Command.link(card_method.id)],
            "available_currency_ids": [Command.link(self.currency.id)],
        }
        if "fees_active" in stripe_provider._fields:
            provider_values["fees_active"] = False
        stripe_provider.write(provider_values)
        tx_sudo = self.env["payment.transaction"].sudo().create({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "reference": "STALE-STRIPE-BATCH",
            "amount": self.invoices._get_multi_invoice_residual(),
            "currency_id": self.currency.id,
            "partner_id": self.partner.id,
            "operation": "online_direct",
            "state": "pending",
            "provider_reference": "pi_stale_batch",
            "invoice_ids": [Command.set(self.invoices.ids)],
            "multi_invoice_batch_key": (
                self.invoices._get_multi_invoice_payment_batch_key()
            ),
        })
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])
        response = self._make_http_get_request(self._build_url(path), params)

        from odoo.addons.payment.models.payment_provider import (
            PaymentProvider,
        )

        with patch.object(
            PaymentProvider,
            "_send_api_request",
            return_value={"id": "pi_stale_batch", "status": "canceled"},
        ) as mocked_cancel:
            refresh_response = self._submit_balance_refresh(response)

        self.assertEqual(refresh_response.status_code, 303)
        tx_sudo.invalidate_recordset(["state"])
        self.assertEqual(tx_sudo.state, "cancel")
        mocked_cancel.assert_called_once()
        self.assertEqual(
            mocked_cancel.call_args.args,
            ("POST", "payment_intents/pi_stale_batch/cancel"),
        )

    def test_balance_refresh_blocks_non_cancelable_pending_attempt(self):
        path, params = self._get_link_parts()
        tx_sudo = self.env["payment.transaction"].sudo().create({
            "provider_id": self.provider.id,
            "payment_method_id": self.payment_method_id,
            "reference": "STALE-REMOTE-BATCH",
            "amount": self.invoices._get_multi_invoice_residual(),
            "currency_id": self.currency.id,
            "partner_id": self.partner.id,
            "operation": "online_direct",
            "state": "pending",
            "provider_reference": "remote_pending_attempt",
            "invoice_ids": [Command.set(self.invoices.ids)],
            "multi_invoice_batch_key": (
                self.invoices._get_multi_invoice_payment_batch_key()
            ),
        })
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])
        response = self._make_http_get_request(self._build_url(path), params)

        refresh_response = self._submit_balance_refresh(response)

        self.assertEqual(refresh_response.status_code, 200)
        self.assertIn(b"could not be safely canceled", refresh_response.content)
        tx_sudo.invalidate_recordset(["state"])
        self.assertEqual(tx_sudo.state, "pending")

    def test_valid_transaction_links_every_invoice(self):
        payment_context = self._get_payment_form_context()
        processing_values = self.make_jsonrpc_request(
            self._build_url(payment_context["transaction_route"]),
            self._prepare_transaction_values(payment_context),
        )
        tx_sudo = self._get_tx(processing_values["reference"])

        self.assertEqual(tx_sudo.invoice_ids, self.invoices)
        self.assertEqual(tx_sudo.amount, self.invoices._get_multi_invoice_residual())
        self.assertTrue(tx_sudo.landing_route.startswith("/payment/confirmation?"))

        confirmation_response = self._make_http_get_request(
            self._build_url(tx_sudo.landing_route)
        )
        self.assertEqual(confirmation_response.status_code, 200)

    def test_repeated_transaction_request_reuses_same_draft(self):
        payment_context = self._get_payment_form_context()
        transaction_values = self._prepare_transaction_values(payment_context)
        tx_model = self.env["payment.transaction"].sudo()
        tx_count = tx_model.search_count([])

        first_values = self.make_jsonrpc_request(
            self._build_url(payment_context["transaction_route"]),
            transaction_values,
        )
        second_values = self.make_jsonrpc_request(
            self._build_url(payment_context["transaction_route"]),
            transaction_values,
        )

        self.assertEqual(first_values["reference"], second_values["reference"])
        self.assertEqual(tx_model.search_count([]), tx_count + 1)
        tx_sudo = self._get_tx(first_values["reference"])
        self.assertTrue(tx_sudo.multi_invoice_batch_key)
        self.assertEqual(tx_sudo.invoice_ids, self.invoices)

    def test_stripe_surcharge_uses_combined_invoice_amount_when_installed(self):
        provider_model = self.env["payment.provider"]
        transaction_model = self.env["payment.transaction"]
        if (
            "fees_active" not in provider_model._fields
            or "fees" not in transaction_model._fields
        ):
            self.skipTest("Stripe surcharge modules are not installed.")

        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider("multi_invoice")
        )

        payment_context = self._get_payment_form_context()
        transaction_values = self._prepare_transaction_values(payment_context)
        transaction_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "stripe_confirmation_token": "ctoken_multi_invoice_credit",
        })

        from odoo.addons.stripe_charge.models.payment_transaction import (
            PaymentTransaction as StripePaymentTransaction,
        )

        with patch.object(
            type(stripe_provider),
            "_stripe_retrieve_confirmation_token",
            return_value={
                "payment_intent": False,
                "expires_at": 4102444800,
                "livemode": False,
                "payment_method_preview": {
                    "type": "card",
                    "card": {
                        "funding": "credit",
                        "brand": "visa",
                        "last4": "4242",
                    },
                },
            },
        ):
            review = self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                transaction_values,
            )
            transaction_values["stripe_surcharge_review_token"] = (
                review["review_token"]
            )
            with patch.object(
                StripePaymentTransaction,
                "_stripe_create_intent",
                return_value={"client_secret": "pi_test_secret"},
            ):
                processing_values = self.make_jsonrpc_request(
                    self._build_url(payment_context["transaction_route"]),
                    transaction_values,
                )
                retry_values = self.make_jsonrpc_request(
                    self._build_url(payment_context["transaction_route"]),
                    transaction_values,
                )

        tx_sudo = self._get_tx(processing_values["reference"])
        self.assertEqual(retry_values["reference"], processing_values["reference"])
        expected_fee = self.currency.round(
            self.invoices._get_multi_invoice_residual() * 0.03
        )
        self.assertEqual(tx_sudo.provider_id, stripe_provider)
        self.assertEqual(tx_sudo.invoice_ids - tx_sudo.fee_move_id, self.invoices)
        self.assertEqual(tx_sudo.fees, expected_fee)
        self.assertTrue(tx_sudo.stripe_surcharge_reviewed)
        self.assertEqual(
            tx_sudo.stripe_confirmation_token_id,
            "ctoken_multi_invoice_credit",
        )
        self.assertEqual(tx_sudo.stripe_card_brand, "visa")
        self.assertEqual(tx_sudo.stripe_card_last4, "4242")
        self.assertEqual(len(tx_sudo.invoice_allocation_snapshot), 2)
        self.assertTrue(tx_sudo.fee_move_id)
        self.assertEqual(tx_sudo.fee_move_id.state, "draft")
        self.assertEqual(
            processing_values["amount"],
            tx_sudo.amount + expected_fee,
        )

        payment_count = self.env["account.payment"].sudo().search_count([])
        tx_sudo._set_done()
        tx_sudo._post_process()

        self.assertTrue(tx_sudo.payment_id)
        self.assertFalse(tx_sudo.fee_payment_id)
        self.assertEqual(
            self.env["account.payment"].sudo().search_count([]),
            payment_count + 1,
        )
        self.assertAlmostEqual(
            tx_sudo.payment_id.amount,
            tx_sudo.amount + tx_sudo.fees,
            2,
        )
        paid_invoices = self.invoices | tx_sudo.fee_move_id
        paid_invoices.invalidate_recordset(["payment_state", "amount_residual"])
        self.assertTrue(all(
            invoice.payment_state in ("paid", "in_payment")
            for invoice in paid_invoices
        ))
        self.assertTrue(all(
            self.currency.is_zero(invoice.amount_residual)
            for invoice in paid_invoices
        ))

    def test_stripe_confirmation_accepts_reordered_base_invoices(self):
        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider(
                "multi_invoice_reordered_confirmation"
            )
        )
        payment_context = self._get_payment_form_context()
        transaction_values = self._prepare_transaction_values(payment_context)
        transaction_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "stripe_confirmation_token": "ctoken_multi_invoice_reordered",
        })
        token_data = {
            "payment_intent": False,
            "expires_at": 4102444800,
            "livemode": False,
            "payment_method_preview": {
                "type": "card",
                "card": {
                    "funding": "credit",
                    "brand": "visa",
                    "last4": "4242",
                },
            },
        }
        intent_id = "pi_multi_invoice_reordered"
        phase_a_payload = {}
        api_calls = []

        def build_intent(status):
            metadata = {
                key[9:-1]: value
                for key, value in phase_a_payload.items()
                if key.startswith("metadata[") and key.endswith("]")
            }
            return {
                "id": intent_id,
                "status": status,
                "confirmation_method": "automatic",
                "amount": int(phase_a_payload["amount"]),
                "amount_received": (
                    int(phase_a_payload["amount"])
                    if status == "succeeded"
                    else 0
                ),
                "currency": phase_a_payload["currency"],
                "description": phase_a_payload["description"],
                "client_secret": f"{intent_id}_secret_test",
                "customer": "cus_multi_invoice_reordered",
                "livemode": False,
                "metadata": metadata,
                "payment_method": (
                    {
                        "id": "pm_multi_invoice_reordered",
                        "type": "card",
                        "card": {
                            "brand": "visa",
                            "funding": "credit",
                            "last4": "4242",
                        },
                    }
                    if status == "succeeded"
                    else None
                ),
                "payment_method_types": ["card"],
            }

        def send_api_request(
            provider,
            method,
            endpoint,
            *,
            data=None,
            **kwargs,
        ):
            api_calls.append((method, endpoint, kwargs.get("idempotency_key")))
            if (method, endpoint) == ("POST", "payment_intents"):
                phase_a_payload.update(data or {})
                return build_intent("requires_payment_method")
            if (method, endpoint) == (
                "GET",
                f"payment_intents/{intent_id}",
            ):
                return build_intent("requires_payment_method")
            if (method, endpoint) == (
                "POST",
                f"payment_intents/{intent_id}/confirm",
            ):
                return build_intent("succeeded")
            self.fail(f"Unexpected Stripe request: {method} {endpoint}")

        transaction_class = type(self.env["payment.transaction"])
        with (
            patch.object(
                type(stripe_provider),
                "_stripe_retrieve_confirmation_token",
                return_value=token_data,
            ),
            patch.object(
                transaction_class,
                "_stripe_create_customer",
                return_value={"id": "cus_multi_invoice_reordered"},
            ),
            patch.object(
                type(stripe_provider),
                "_send_api_request",
                new=send_api_request,
            ),
        ):
            review = self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                transaction_values,
            )
            transaction_values["stripe_surcharge_review_token"] = (
                review["review_token"]
            )
            processing_values = self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                transaction_values,
            )
            tx_sudo = self._get_tx(processing_values["reference"])
            canonical_invoice_ids = sorted(self.invoices.ids)
            phase_a_metadata = build_intent(
                "requires_payment_method"
            )["metadata"]

            self.assertTrue(tx_sudo.fee_move_id)
            self.assertEqual(
                tx_sudo.invoice_ids - tx_sudo.fee_move_id,
                self.invoices,
            )
            self.assertEqual(
                phase_a_metadata["odoo_invoice_ids"],
                ",".join(
                    str(invoice_id) for invoice_id in canonical_invoice_ids
                ),
            )
            reverse_base_invoices = self.env["account.move"].browse(
                list(reversed(canonical_invoice_ids))
            )
            with patch.object(
                transaction_class,
                "_get_receipt_base_invoices",
                return_value=reverse_base_invoices,
            ):
                confirmation = self.make_jsonrpc_request(
                    self._build_url(
                        processing_values["stripe_confirmation_route"]
                    ),
                    {
                        "transaction_id": tx_sudo.id,
                        "confirmation_signature": processing_values[
                            "stripe_confirmation_signature"
                        ],
                    },
                )

        tx_sudo.invalidate_recordset(["payment_id", "state"])
        paid_invoices = self.invoices | tx_sudo.fee_move_id
        paid_invoices.invalidate_recordset([
            "amount_residual",
            "payment_state",
        ])
        self.assertEqual(confirmation["state"], "done")
        self.assertEqual(tx_sudo.state, "done")
        self.assertTrue(tx_sudo.payment_id)
        self.assertTrue(all(
            invoice.payment_state in ("paid", "in_payment")
            and self.currency.is_zero(invoice.amount_residual)
            for invoice in paid_invoices
        ))
        self.assertEqual(
            [(method, endpoint) for method, endpoint, _key in api_calls],
            [
                ("POST", "payment_intents"),
                ("GET", f"payment_intents/{intent_id}"),
                ("POST", f"payment_intents/{intent_id}/confirm"),
            ],
        )
        self.assertTrue(api_calls[0][2])
        self.assertTrue(api_calls[2][2])

    def test_abandoned_multi_invoice_card_attempt_is_safely_replaced(self):
        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider("multi_invoice_replace")
        )
        payment_context = self._get_payment_form_context()
        first_values = self._prepare_transaction_values(payment_context)
        first_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "stripe_confirmation_token": "ctoken_multi_first",
        })
        token_data = {
            "payment_intent": False,
            "expires_at": 4102444800,
            "livemode": False,
            "payment_method_preview": {
                "type": "card",
                "card": {
                    "funding": "credit",
                    "brand": "visa",
                    "last4": "4242",
                },
            },
        }
        transaction_class = type(self.env["payment.transaction"])
        created_intents = {"count": 0}

        def create_intent(tx):
            created_intents["count"] += 1
            intent_id = f"pi_multi_replace_{created_intents['count']}"
            tx.provider_reference = intent_id
            return {
                "id": intent_id,
                "status": "requires_payment_method",
                "confirmation_method": "automatic",
                "client_secret": f"{intent_id}_secret",
            }

        with (
            patch.object(
                type(stripe_provider),
                "_stripe_retrieve_confirmation_token",
                return_value=token_data,
            ),
            patch.object(
                transaction_class,
                "_stripe_create_intent",
                new=create_intent,
            ),
        ):
            first_review = self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                first_values,
            )
            first_values["stripe_surcharge_review_token"] = (
                first_review["review_token"]
            )
            first_processing = self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                first_values,
            )
            first_tx = self._get_tx(first_processing["reference"])

            second_values = {
                **first_values,
                "stripe_confirmation_token": "ctoken_multi_second",
            }
            second_review = self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                second_values,
            )
            second_values["stripe_surcharge_review_token"] = (
                second_review["review_token"]
            )

            def send_api_request(provider, method, endpoint, *, data=None, **kwargs):
                intent = {
                    "id": first_tx.provider_reference,
                    "status": (
                        "canceled" if method == "POST" else "requires_payment_method"
                    ),
                    "confirmation_method": "automatic",
                    "amount": payment_utils.to_minor_currency_units(
                        first_tx.amount + first_tx.fees,
                        first_tx.currency_id,
                    ),
                    "currency": first_tx.currency_id.name.lower(),
                    "description": first_tx.reference,
                    "metadata": first_tx._get_stripe_payment_intent_metadata(),
                }
                return intent

            with patch.object(
                type(stripe_provider),
                "_send_api_request",
                new=send_api_request,
            ):
                cancellation_pending = self.make_jsonrpc_request(
                    self._build_url(payment_context["transaction_route"]),
                    second_values,
                )
                second_processing = self.make_jsonrpc_request(
                    self._build_url(payment_context["transaction_route"]),
                    second_values,
                )

        first_tx.invalidate_recordset([
            "state",
            "stripe_reviewed_cancellation_requested_at",
        ])
        second_tx = self._get_tx(second_processing["reference"])
        self.assertEqual(cancellation_pending["state"], "error")
        self.assertEqual(cancellation_pending["reference"], first_tx.reference)
        self.assertNotIn("stripe_confirmation_required", cancellation_pending)
        self.assertTrue(first_tx.stripe_reviewed_cancellation_requested_at)
        self.assertEqual(first_tx.state, "cancel")
        self.assertNotEqual(second_tx, first_tx)
        self.assertEqual(second_tx.provider_reference, "pi_multi_replace_2")

    def test_saved_stripe_card_commits_reviewed_tx_before_charge(self):
        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider("multi_invoice_token")
        )
        payment_token = self.env["payment.token"].sudo().create({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "partner_id": self.partner.id,
            "provider_ref": "cus_multi_invoice",
            "payment_details": "4242",
            "stripe_payment_method": "pm_multi_invoice_credit",
        })
        payment_method_data = {
            "id": "pm_multi_invoice_credit",
            "type": "card",
            "card": {
                "funding": "credit",
                "brand": "visa",
                "last4": "4242",
            },
        }
        self.portal_user.partner_id = self.partner
        self.authenticate(self.portal_user.login, self.portal_user.login)
        payment_context = self._get_payment_form_context()
        self.assertEqual(payment_context["partner_id"], self.partner.id)
        self.assertIn(payment_token.id, payment_context["token_ids"])
        transaction_values = self._prepare_transaction_values(payment_context)
        transaction_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": None,
            "token_id": payment_token.id,
            "flow": "token",
        })

        from odoo.addons.stripe_charge.models.payment_transaction import (
            PaymentTransaction as StripePaymentTransaction,
        )
        transaction_model_class = type(self.env["payment.transaction"])

        with patch.object(
            type(stripe_provider),
            "_stripe_retrieve_payment_method",
            return_value=payment_method_data,
        ):
            review = self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                transaction_values,
            )
            transaction_values["stripe_surcharge_review_token"] = (
                review["review_token"]
            )
            with (
                patch.object(
                    transaction_model_class,
                    "_charge_with_token",
                ) as mocked_charge,
                patch.object(
                    StripePaymentTransaction,
                    "_stripe_create_intent",
                    return_value={
                        "id": "pi_multi_invoice_token",
                        "status": "requires_confirmation",
                        "client_secret": "pi_multi_invoice_token_secret",
                    },
                ),
            ):
                processing_values = self.make_jsonrpc_request(
                    self._build_url(payment_context["transaction_route"]),
                    transaction_values,
                )

        tx_sudo = self._get_tx(processing_values["reference"])
        mocked_charge.assert_not_called()
        self.assertEqual(tx_sudo.operation, "online_token")
        self.assertEqual(tx_sudo.token_id, payment_token)
        self.assertEqual(tx_sudo.state, "draft")
        self.assertTrue(tx_sudo.stripe_surcharge_reviewed)
        self.assertFalse(tx_sudo.stripe_confirmation_token_id)
        self.assertEqual(tx_sudo.stripe_card_funding, "credit")
        self.assertEqual(tx_sudo.stripe_card_brand, "visa")
        self.assertEqual(tx_sudo.stripe_card_last4, "4242")
        self.assertTrue(tx_sudo.fee_move_id)
        self.assertEqual(
            tx_sudo.fees,
            self.currency.round(
                self.invoices._get_multi_invoice_residual() * 0.03
            ),
        )

    def test_public_link_holder_cannot_use_saved_card(self):
        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider("multi_invoice_public")
        )
        payment_token = self.env["payment.token"].sudo().create({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "partner_id": self.partner.id,
            "provider_ref": "cus_multi_invoice_public",
            "payment_details": "4242",
            "stripe_payment_method": "pm_multi_invoice_public",
        })
        payment_context = self._get_payment_form_context()
        self.assertNotIn(payment_token.id, payment_context["token_ids"])
        transaction_values = self._prepare_transaction_values(payment_context)
        transaction_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": None,
            "token_id": payment_token.id,
            "flow": "token",
        })

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                transaction_values,
            )
        transaction_values["stripe_surcharge_review_token"] = (
            "forged.review.token"
        )
        tx_count = self.env["payment.transaction"].sudo().search_count([])
        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                transaction_values,
            )
        self.assertEqual(
            self.env["payment.transaction"].sudo().search_count([]),
            tx_count,
        )

    def test_escalated_sale_surcharge_blocks_multi_invoice_payment(self):
        product = self.env["product.product"].create({
            "name": "Escalated Multi-Invoice Surcharge",
            "type": "service",
            "sale_ok": True,
            "taxes_id": [Command.clear()],
        })
        pricelist = self.env["product.pricelist"].create({
            "name": "Escalated Multi-Invoice Currency",
            "currency_id": self.currency.id,
        })
        order = self.env["sale.order"].sudo().create({
            "partner_id": self.partner.id,
            "pricelist_id": pricelist.id,
            "order_line": [Command.create({
                "product_id": product.id,
                "product_uom_qty": 1.0,
                "price_unit": 25.0,
                "is_stripe_surcharge": True,
            })],
        })
        order.action_confirm()
        affected_invoice = order._create_invoices()
        affected_invoice.action_post()
        order.order_line.stripe_surcharge_accounting_escalated = True
        invoices = affected_invoice | self.invoice_2

        path, params = self._get_link_parts(invoices)
        response = self._make_http_get_request(self._build_url(path), params)
        self.assertEqual(response.status_code, 200)
        payment_context = self._get_payment_context(response)
        stripe_provider, card_method = (
            self._configure_stripe_surcharge_provider(
                "blocked_multi_invoice"
            )
        )
        review_values = self._prepare_transaction_values(payment_context)
        review_values.update({
            "provider_id": stripe_provider.id,
            "payment_method_id": card_method.id,
            "stripe_confirmation_token": "ctoken_blocked_multi_invoice",
        })

        with (
            patch.object(
                type(stripe_provider),
                "_stripe_retrieve_confirmation_token",
            ) as retrieve_token,
            self.assertRaises(JsonRpcException),
        ):
            self.make_jsonrpc_request(
                self._build_url(payment_context["stripe_review_route"]),
                review_values,
            )
        retrieve_token.assert_not_called()

        transaction_values = self._prepare_transaction_values(payment_context)
        tx_count = self.env["payment.transaction"].sudo().search_count([])
        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                transaction_values,
            )
        self.assertEqual(
            self.env["payment.transaction"].sudo().search_count([]),
            tx_count,
        )

    def test_orphaned_multi_invoice_capture_recovers_to_exact_invoice_batch(self):
        stripe_provider, _card_method = (
            self._configure_stripe_surcharge_provider("multi_invoice_recovery")
        )
        invoice_ids = ",".join(
            str(invoice_id) for invoice_id in sorted(self.invoices.ids)
        )
        metadata = {
            "odoo_database_uuid": (
                self.env["ir.config_parameter"].sudo().get_param("database.uuid")
                or ""
            ),
            "odoo_company_id": str(self.company.id),
            "odoo_provider_id": str(stripe_provider.id),
            "odoo_transaction_id": "999999997",
            "odoo_transaction_reference": "MISSING-MULTI-INVOICE",
            "odoo_invoice_ids": invoice_ids,
            "odoo_invoice_count": str(len(self.invoices)),
            "odoo_partner_id": str(self.partner.commercial_partner_id.id),
        }
        amount_minor = payment_utils.to_minor_currency_units(
            self.invoices._get_multi_invoice_residual(),
            self.currency,
        )
        event = self.env["stripe.online.webhook.event"].sudo().create({
            "name": "evt_multi_invoice_recovery",
            "provider_id": stripe_provider.id,
            "event_type": "payment_intent.succeeded",
            "payment_intent_id": "pi_multi_invoice_recovery",
            "livemode": False,
            "amount_received_minor": amount_minor,
            "currency_code": self.currency.name,
            "odoo_metadata": metadata,
            "state": "unmatched",
        })
        intent = {
            "id": event.payment_intent_id,
            "status": "succeeded",
            "amount": amount_minor,
            "amount_received": amount_minor,
            "currency": self.currency.name.lower(),
            "description": "MISSING-MULTI-INVOICE",
            "livemode": False,
            "metadata": metadata,
            "payment_method": {
                "id": "pm_multi_invoice_recovery",
                "type": "card",
                "card": {
                    "funding": "credit",
                    "brand": "visa",
                    "last4": "4242",
                },
            },
            "payment_method_types": ["card"],
        }
        self.env.ref("account.group_account_manager").sudo().write({
            "user_ids": [Command.link(self.env.user.id)],
        })
        wizard = self.env[
            "stripe.online.payment.recovery.wizard"
        ].create({
            "event_id": event.id,
            "invoice_ids": [Command.set(self.invoices.ids)],
            "recovery_note": "Verified the complete invoice batch in Stripe.",
            "confirm_already_captured": True,
        })
        original_residual = self.invoices._get_multi_invoice_residual()

        with patch.object(
            type(stripe_provider),
            "_send_api_request",
            return_value=intent,
        ):
            wizard.action_recover_payment()

        event.invalidate_recordset(["invoice_ids", "recovered_payment_id", "state"])
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])
        payment = event.recovered_payment_id
        self.assertEqual(event.state, "recovered")
        self.assertEqual(event.invoice_ids, self.invoices)
        self.assertEqual(payment.stripe_recovery_invoice_ids, self.invoices)
        self.assertFalse(payment.sale_order_id)
        self.assertEqual(payment.partner_id, self.partner.commercial_partner_id)
        self.assertEqual(
            self.invoices._get_multi_invoice_residual(),
            original_residual,
        )

    def test_unapplied_recovered_receipt_blocks_batch_transaction(self):
        stripe_provider, _card_method = (
            self._configure_stripe_surcharge_provider(
                "multi_invoice_recovery_block"
            )
        )
        amount = self.invoices._get_multi_invoice_residual()
        amount_minor = payment_utils.to_minor_currency_units(
            amount,
            self.currency,
        )
        event = self.env["stripe.online.webhook.event"].sudo().create({
            "name": "evt_multi_invoice_recovery_block",
            "provider_id": stripe_provider.id,
            "event_type": "payment_intent.succeeded",
            "payment_intent_id": "pi_multi_invoice_recovery_block",
            "livemode": False,
            "amount_received_minor": amount_minor,
            "currency_code": self.currency.name,
            "invoice_ids": [Command.set(self.invoices.ids)],
            "state": "unmatched",
        })
        payment_method_line = (
            stripe_provider.journal_id.inbound_payment_method_line_ids.filtered(
                lambda line: line.payment_provider_id == stripe_provider
            )[:1]
        )
        self.assertTrue(payment_method_line)
        payment = self.env["account.payment"].sudo().create({
            "amount": amount,
            "payment_type": "inbound",
            "currency_id": self.currency.id,
            "partner_id": self.partner.commercial_partner_id.id,
            "partner_type": "customer",
            "journal_id": stripe_provider.journal_id.id,
            "company_id": self.company.id,
            "payment_method_line_id": payment_method_line.id,
            "memo": "Recovered Stripe receipt for transaction guard test.",
            "stripe_recovery_invoice_ids": [Command.set(self.invoices.ids)],
            "stripe_external_payment_intent_id": event.payment_intent_id,
            "stripe_recovery_event_id": event.id,
        })
        payment.action_post()
        event.write({
            "state": "recovered",
            "recovered_payment_id": payment.id,
        })
        self.assertEqual(
            self.invoices._get_unapplied_stripe_recovery_payments(),
            payment,
        )
        payment_context = self._get_payment_form_context()
        tx_model = self.env["payment.transaction"].sudo()
        tx_count = tx_model.search_count([])

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                self._prepare_transaction_values(payment_context),
            )

        self.assertEqual(tx_model.search_count([]), tx_count)

    def test_transaction_rejects_tampered_amount(self):
        payment_context = self._get_payment_form_context()
        transaction_values = self._prepare_transaction_values(payment_context)
        transaction_values["amount"] += 1.0
        tx_count = self.env["payment.transaction"].sudo().search_count([])

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                transaction_values,
            )

        self.assertEqual(
            self.env["payment.transaction"].sudo().search_count([]),
            tx_count,
        )

    def test_transaction_rejects_invoice_substitution(self):
        replacement = self.init_invoice(
            "out_invoice",
            self.partner,
            amounts=[60.0],
            currency=self.currency,
        )
        replacement.action_post()
        payment_context = self._get_payment_form_context()
        substituted_ids = ",".join(
            str(invoice_id)
            for invoice_id in sorted((self.invoice_1 | replacement).ids)
        )
        transaction_route = (
            f"/payment/pay/multi_invoice/transaction/{substituted_ids}"
        )
        tx_count = self.env["payment.transaction"].sudo().search_count([])

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(transaction_route),
                self._prepare_transaction_values(payment_context),
            )

        self.assertEqual(
            self.env["payment.transaction"].sudo().search_count([]),
            tx_count,
        )

    def test_transaction_rejects_stale_balance(self):
        payment_context = self._get_payment_form_context()
        self._register_payment(
            self.invoice_1,
            amount=10.0,
            journal_id=self.company_data["default_journal_bank"].id,
        )
        self.invoices.invalidate_recordset(["amount_residual", "payment_state"])
        tx_count = self.env["payment.transaction"].sudo().search_count([])

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                self._build_url(payment_context["transaction_route"]),
                self._prepare_transaction_values(payment_context),
            )

        self.assertEqual(
            self.env["payment.transaction"].sudo().search_count([]),
            tx_count,
        )
