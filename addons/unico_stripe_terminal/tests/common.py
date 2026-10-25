from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from odoo.addons.unico_stripe_terminal.models import stripe_client


class StripeTerminalCommon(AccountTestInvoicingCommon):
    """Shared fixtures + a helper to swap the real `stripe` module for a mock.

    Built on :class:`AccountTestInvoicingCommon` so a chart of accounts and
    journals exist - this lets ``account.payment.action_post()`` run for real.
    No network calls are made: every test that touches Stripe runs inside
    ``self._patch_stripe(...)`` which replaces ``stripe_client.stripe`` with a
    configured :class:`MagicMock`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.journal = cls.company_data["default_journal_bank"]

        # The accounting test user lacks Sales and Stripe Terminal access; grant
        # both so the fixtures (sale.order) and manager-only terminal models work.
        for xmlid in ("sales_team.group_sale_manager",
                      "unico_stripe_terminal.group_stripe_terminal_manager"):
            cls.env.ref(xmlid).sudo().write({"user_ids": [(4, cls.env.user.id)]})

        # Reuse / create the Odoo Stripe payment provider as the credential store.
        Provider = cls.env["payment.provider"]
        cls.provider = Provider.search([
            ("code", "=", "stripe"), ("company_id", "=", cls.company.id)], limit=1)
        if not cls.provider:
            cls.provider = Provider.create({
                "name": "Stripe Terminal Test",
                "code": "stripe",
                "company_id": cls.company.id,
            })
        # Cash is configured to share the card journal, told apart only by its
        # payment method line -- the setup the Cash Journal / Cash Payment
        # Method provider settings exist for.
        cls.cash_method_line = cls.env["account.payment.method.line"].create({
            "name": "Cash",
            "payment_method_id": cls.env.ref(
                "account.account_payment_method_manual_in").id,
            "journal_id": cls.journal.id,
        })
        cls.provider.write({
            "state": "test",
            "stripe_secret_key": "sk_test_dummy",
            "stripe_publishable_key": "pk_test_dummy",
            "stripe_webhook_secret": "whsec_dummy",
            "journal_id": cls.journal.id,
            "unico_cash_journal_id": cls.journal.id,
            "unico_cash_payment_method_line_id": cls.cash_method_line.id,
        })

        cls.partner = cls.partner_a
        cls.product = cls.product_a
        cls.surcharge_product = cls.env["product.product"].create({
            "name": "Stripe Processing Surcharge - 3%",
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
            "company_id": cls.company.id,
            "property_account_income_id": cls.company_data[
                "default_account_revenue"
            ].id,
            "taxes_id": [(5, 0, 0)],
        })
        cls.location = cls.env["stripe.terminal.location"].create({
            "name": "Main Store",
            "stripe_location_id": "tml_main",
            "company_id": cls.company.id,
        })
        cls.reader = cls.env["stripe.terminal.reader"].create({
            "label": "Front Desk",
            "stripe_reader_id": "tmr_front",
            "status": "online",
            "is_simulated": True,
            "location_id": cls.location.id,
            "company_id": cls.company.id,
        })
        cls.sale_order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "order_line": [(0, 0, {
                "product_id": cls.product.id,
                "product_uom_qty": 1.0,
                "price_unit": 100.0,
            })],
        })

    # ------------------------------------------------------------------
    # Stripe mock helpers
    # ------------------------------------------------------------------
    def _build_mock_stripe(self, create_status="requires_payment_method",
                           retrieve_status="succeeded", charge_id="ch_test_1",
                           last_error=None, refund_amount=10000,
                           refund_status="succeeded",
                           intent_id="pi_test_123", funding="credit",
                           collect_before_confirm=False):
        """Return a MagicMock mimicking the bits of `stripe` we call."""
        m = MagicMock(name="stripe")
        m.PaymentIntent.create.return_value = {"id": intent_id, "status": create_status}
        retrieve = {
            "id": intent_id,
            "status": retrieve_status,
            "latest_charge": charge_id,
        }
        if last_error:
            retrieve["last_payment_error"] = {"message": last_error}
        if collect_before_confirm:
            collected = {
                "id": intent_id,
                "status": "requires_confirmation",
                "latest_charge": None,
                "payment_method": {
                    "id": "pm_terminal_test",
                    "type": "card_present",
                    "card_present": {"funding": funding},
                },
            }
            completed = {
                **retrieve,
                "payment_method": collected["payment_method"],
            }
            m.PaymentIntent.retrieve.side_effect = [collected, completed]
        else:
            m.PaymentIntent.retrieve.return_value = retrieve
        m.PaymentMethod.retrieve.return_value = {
            "id": "pm_terminal_test",
            "type": "card_present",
            "card_present": {"funding": funding},
        }
        m.PaymentIntent.cancel.return_value = {"id": intent_id, "status": "canceled"}
        m.Refund.create.return_value = {
            "id": "re_test_1",
            "amount": refund_amount,
            "status": refund_status,
        }
        m.Refund.retrieve.return_value = m.Refund.create.return_value
        return m

    @contextmanager
    def _patch_stripe(self, mock=None):
        mock = mock or self._build_mock_stripe()
        with patch.object(stripe_client, "stripe", mock):
            yield mock

    def _make_payment(self, amount=100.0, reader=None):
        return self.env["stripe.terminal.payment"].create({
            "sale_order_id": self.sale_order.id,
            "partner_id": self.partner.id,
            "amount": amount,
            "currency_id": self.sale_order.currency_id.id,
            "reader_id": (reader or self.reader).id,
            "company_id": self.company.id,
        })

    def _collect_cash(self, amount):
        """Run the wizard's cash path and return the created account.payment."""
        wizard = self.env["stripe.terminal.payment.wizard"].create({
            "payment_method": "cash",
            "sale_order_id": self.sale_order.id,
            "partner_id": self.partner.id,
            "amount": amount,
            "currency_id": self.sale_order.currency_id.id,
        })
        action = wizard.action_collect()
        if action.get("res_model") == "account.payment":
            return self.env["account.payment"].browse(action["res_id"])
        return self.env["account.payment"].sudo().search(
            [("sale_order_id", "=", self.sale_order.id)],
            order="id desc",
            limit=1,
        )

    def _post_invoice(self):
        """Confirm the order, create and post its customer invoice."""
        self.product.product_tmpl_id.invoice_policy = "order"
        self.sale_order.action_confirm()
        invoice = self.sale_order._create_invoices()
        invoice.action_post()
        return invoice

    def _enable_fees(self, var=0.029, fixed=0.30):
        """Turn on the customer surcharge with its Accounting product."""
        fee_journal = self.env["account.journal"].create({
            "name": "Stripe Fees",
            "type": "bank",
            "code": "STFEE",
            "company_id": self.company.id,
        })
        self.provider.write({
            "fees_active": True,
            "fees_dom_var": var,
            "fees_dom_fixed": fixed,
            "fees_int_var": var,
            "fees_int_fixed": fixed,
            "stripe_card_fee_journal_id": fee_journal.id,
            "stripe_surcharge_product_id": self.surcharge_product.id,
        })
        return fee_journal
