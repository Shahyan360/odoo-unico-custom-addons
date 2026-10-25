from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.unico_stripe_terminal.models import stripe_client

from .common import StripeTerminalCommon


class _FakeStripeError(Exception):
    """Mimics the attributes stripe-python errors expose."""

    def __init__(self, message, code, request_id="req_test_123"):
        super().__init__(f"Request {request_id}: {message}")
        self.code = code
        self.request_id = request_id
        self.user_message = message


@tagged("post_install", "-at_install")
class TestStripeTerminalFlow(StripeTerminalCommon):

    def test_collect_via_wizard_happy_path(self):
        wizard = self.env["stripe.terminal.payment.wizard"].create({
            "sale_order_id": self.sale_order.id,
            "partner_id": self.partner.id,
            "amount": 100.0,
            "currency_id": self.sale_order.currency_id.id,
            "reader_id": self.reader.id,
        })
        with self._patch_stripe(self._build_mock_stripe(retrieve_status="succeeded")):
            wizard.action_collect()
        payment = self.sale_order.stripe_terminal_payment_ids
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.status, "succeeded")
        self.assertTrue(payment.account_payment_id)
        self.assertAlmostEqual(payment.account_payment_id.amount, 100.0, 2)
        self.assertEqual(payment.account_payment_id.move_id.state, "posted")
        # Order auto-confirmed on success.
        self.assertEqual(self.sale_order.state, "sale")

    def test_process_on_reader_creates_intent_and_pushes(self):
        payment = self._make_payment()
        with self._patch_stripe(self._build_mock_stripe(retrieve_status="succeeded")) as m:
            payment._process_on_reader()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.payment_intent_id, "pi_test_123")
        self.assertEqual(payment.charge_id, "ch_test_1")
        self.assertEqual(
            payment.account_payment_id.stripe_external_payment_intent_id,
            payment.payment_intent_id,
        )
        m.PaymentIntent.create.assert_called_once()
        m.terminal.Reader.process_payment_intent.assert_called_once()

    def test_terminal_intent_is_stamped_only_on_canonical_payment(self):
        terminal_payment = self._make_payment(amount=75.0)
        terminal_payment.payment_intent_id = "pi_terminal_canonical_payment"
        journal = terminal_payment._get_journal()
        first_payment = self.sale_order._stripe_register_customer_payment(
            50.0,
            journal,
            "First accounting receipt",
        )
        second_payment = self.sale_order._stripe_register_customer_payment(
            25.0,
            journal,
            "Second accounting receipt",
        )
        payments = first_payment | second_payment

        with patch.object(
            type(self.sale_order),
            "_stripe_register_customer_payment",
            return_value=payments,
        ):
            result = terminal_payment._register_account_payment()

        payments.invalidate_recordset(["stripe_external_payment_intent_id"])
        terminal_payment.invalidate_recordset(["account_payment_id"])
        self.assertEqual(result, payments)
        self.assertEqual(terminal_payment.account_payment_id, first_payment)
        self.assertEqual(
            first_payment.stripe_external_payment_intent_id,
            "pi_terminal_canonical_payment",
        )
        self.assertFalse(second_payment.stripe_external_payment_intent_id)

    def test_process_on_offline_reader_raises(self):
        self.reader.status = "offline"
        payment = self._make_payment()
        with self._patch_stripe():
            with self.assertRaises(UserError) as caught:
                payment._process_on_reader()
        message = str(caught.exception)
        self.assertIn("isn't connected", message)
        self.assertIn("marked offline in Odoo", message)
        self.assertEqual(payment.status, "draft")

    def _reader_command_failing_with(self, error):
        mock = self._build_mock_stripe()
        mock.terminal.Reader.process_payment_intent.side_effect = error
        mock.terminal.Reader.collect_payment_method.side_effect = error
        return mock

    @mute_logger("odoo.addons.unico_stripe_terminal.models.stripe_terminal_payment")
    def test_reader_error_codes_show_friendly_message(self):
        cases = {
            "terminal_reader_timeout": "Check the terminal screen",
            "terminal_reader_busy": "busy with another action",
            "terminal_reader_offline": "isn't connected",
        }
        for code, expected_text in cases.items():
            with self.subTest(code=code):
                error = _FakeStripeError(
                    "There was a timeout when sending this command to the reader.",
                    code=code,
                )
                payment = self._make_payment()
                with self._patch_stripe(self._reader_command_failing_with(error)):
                    with self.assertRaises(UserError) as caught:
                        payment._process_on_reader()
                message = str(caught.exception)
                self.assertIn(expected_text, message)
                self.assertIn(f"\n\nRef: {code} · req_test_123\n", message)
                self.assertIn("timeout when sending this command", message)

    @mute_logger("odoo.addons.unico_stripe_terminal.models.stripe_terminal_payment")
    def test_unknown_reader_error_falls_back_to_generic_message(self):
        payment = self._make_payment()
        error = Exception("Something unexpected")
        with self._patch_stripe(self._reader_command_failing_with(error)):
            with self.assertRaises(UserError) as caught:
                payment._process_on_reader()
        message = str(caught.exception)
        self.assertIn("starting the payment on the terminal", message)
        self.assertTrue(message.endswith("\n\nSomething unexpected"))

    def test_failed_payment_records_error(self):
        payment = self._make_payment()
        mock = self._build_mock_stripe(
            create_status="requires_payment_method",
            retrieve_status="requires_payment_method",
            charge_id=None,
            last_error="Your card was declined.",
        )
        with self._patch_stripe(mock):
            payment._process_on_reader()
        self.assertEqual(payment.status, "failed")
        self.assertEqual(payment.error_message, "Your card was declined.")
        self.assertFalse(payment.account_payment_id)

    def test_cancel_in_progress_payment(self):
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        with self._patch_stripe() as m:
            payment.action_cancel()
        self.assertEqual(payment.status, "canceled")
        m.PaymentIntent.cancel.assert_called_once()

    def test_refund_succeeded_payment(self):
        payment = self._make_payment()
        payment.write({"status": "succeeded", "payment_intent_id": "pi_test_123"})
        with self._patch_stripe(self._build_mock_stripe(refund_amount=10000)) as m:
            payment._process_refund(100.0)
        self.assertEqual(payment.status, "refunded")
        self.assertAlmostEqual(payment.refunded_amount, 100.0, 2)
        self.assertEqual(len(payment.refund_ids), 1)
        self.assertEqual(payment.refund_ids.status, "succeeded")
        m.Refund.create.assert_called_once_with(
            payment_intent="pi_test_123",
            amount=10000,
            idempotency_key=f"terminal_refund_{payment.id}_10000",
        )

    def test_pending_refund_waits_for_stripe_before_accounting(self):
        payment = self._make_payment()
        payment.write({
            "status": "succeeded",
            "payment_intent_id": "pi_pending_refund",
        })
        with self._patch_stripe(self._build_mock_stripe(
            intent_id="pi_pending_refund",
            refund_amount=10000,
            refund_status="pending",
        )):
            refund = payment._process_refund(100.0)

        self.assertEqual(refund.status, "pending")
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.refunded_amount, 0.0)
        self.assertFalse(refund.account_payment_id)
        self.assertFalse(refund.surcharge_refund_move_id)
        self.assertEqual(payment._get_refundable_base_amount(), 0.0)

        refund._apply_stripe_status({
            "id": refund.stripe_refund_id,
            "amount": 10000,
            "status": "succeeded",
        })

        self.assertEqual(refund.status, "succeeded")
        self.assertEqual(payment.status, "refunded")
        self.assertEqual(payment.refunded_amount, 100.0)

    def test_failed_pending_refund_releases_refund_capacity(self):
        payment = self._make_payment()
        payment.write({
            "status": "succeeded",
            "payment_intent_id": "pi_failed_refund",
        })
        with self._patch_stripe(self._build_mock_stripe(
            intent_id="pi_failed_refund",
            refund_amount=10000,
            refund_status="pending",
        )):
            refund = payment._process_refund(100.0)

        refund._apply_stripe_status({
            "id": refund.stripe_refund_id,
            "amount": 10000,
            "status": "failed",
            "failure_reason": "declined",
        })

        self.assertEqual(refund.status, "failed")
        self.assertEqual(payment.refunded_amount, 0.0)
        self.assertEqual(payment._get_refundable_base_amount(), 100.0)
        self.assertFalse(refund.account_payment_id)

    def test_refund_action_opens_wizard(self):
        payment = self._make_payment()
        payment.write({"status": "succeeded", "payment_intent_id": "pi_test_123"})
        action = payment.action_refund()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertEqual(action["res_model"], "stripe.terminal.refund.wizard")
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["context"], {"default_payment_id": payment.id})

    def test_refund_requires_succeeded(self):
        payment = self._make_payment()
        payment.status = "processing"
        with self.assertRaises(UserError):
            payment.action_refund()

    def test_apply_intent_status_succeeded_registers_payment(self):
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        # Simulates what the webhook controller does on payment_intent.succeeded.
        payment._apply_intent_status({
            "id": "pi_test_123",
            "status": "succeeded",
            "latest_charge": "ch_test_1",
        })
        self.assertEqual(payment.status, "succeeded")
        self.assertTrue(payment.account_payment_id)
        self.assertEqual(self.sale_order.state, "sale")

    def test_on_succeeded_is_idempotent(self):
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        intent = {"id": "pi_test_123", "status": "succeeded", "latest_charge": "ch_test_1"}
        payment._apply_intent_status(intent)
        first_payment = payment.account_payment_id
        # A duplicate webhook / poll must not create a second account.payment.
        payment._apply_intent_status(intent)
        self.assertEqual(payment.account_payment_id, first_payment)

    def test_payment_intent_id_must_be_unique(self):
        first_payment = self._make_payment()
        second_payment = self._make_payment()
        first_payment.payment_intent_id = "pi_unique_terminal"

        with self.assertRaisesRegex(ValidationError, "already linked"):
            second_payment.payment_intent_id = "pi_unique_terminal"

        with self.assertRaisesRegex(ValidationError, "already linked"):
            self.env["stripe.terminal.payment"].create({
                "sale_order_id": self.sale_order.id,
                "partner_id": self.partner.id,
                "amount": 50.0,
                "currency_id": self.sale_order.currency_id.id,
                "reader_id": self.reader.id,
                "company_id": self.company.id,
                "payment_intent_id": " pi_unique_terminal ",
            })

    def test_on_succeeded_reloads_existing_payment_after_lock(self):
        payment = self._make_payment()
        payment.write({"status": "succeeded", "payment_intent_id": "pi_test_123"})
        self.assertFalse(payment.account_payment_id)
        existing_payment = self.sale_order._stripe_register_customer_payment(
            payment.amount,
            payment._get_journal(),
            "Existing payment - %s" % payment.name,
        )
        self.env.cr.execute(
            'UPDATE "%s" SET account_payment_id = %%s WHERE id = %%s'
            % payment._table,
            [existing_payment.id, payment.id],
        )

        payment._on_succeeded()

        payment.invalidate_recordset(["account_payment_id"])
        self.assertEqual(payment.account_payment_id, existing_payment)
        collected = self.env["account.payment"].search([
            ("sale_order_id", "=", self.sale_order.id),
            ("payment_type", "=", "inbound"),
        ])
        self.assertEqual(collected, existing_payment)

    def test_on_succeeded_reloads_order_state_after_lock(self):
        payment = self._make_payment()
        payment.write({"status": "succeeded", "payment_intent_id": "pi_test_123"})
        order = self.sale_order
        # Prime the cache with the pre-lock draft state, then mimic another
        # worker confirming the order before this worker obtains the lock.
        self.assertEqual(order.state, "draft")
        existing_payment = order._stripe_register_customer_payment(
            payment.amount,
            payment._get_journal(),
            "Existing payment - %s" % payment.name,
        )
        self.env.cr.execute(
            'UPDATE "%s" SET state = %%s WHERE id = %%s'
            % order._table,
            ["sale", order.id],
        )
        self.env.cr.execute(
            'UPDATE "%s" SET account_payment_id = %%s WHERE id = %%s'
            % payment._table,
            [existing_payment.id, payment.id],
        )

        with patch.object(type(order), "action_confirm") as mock_confirm:
            payment._on_succeeded()

        mock_confirm.assert_not_called()
        order.invalidate_recordset(["state"])
        self.assertEqual(order.state, "sale")

    def test_payment_reconciles_open_invoice(self):
        # Invoice the confirmed order, then collect the full amount.
        self.product.product_tmpl_id.invoice_policy = "order"
        order = self.sale_order
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self.assertEqual(invoice.payment_state, "not_paid")

        payment = self._make_payment(amount=invoice.amount_total)
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        payment._apply_intent_status({
            "id": "pi_test_123",
            "status": "succeeded",
            "latest_charge": "ch_test_1",
        })

        self.assertEqual(payment.status, "succeeded")
        self.assertTrue(payment.account_payment_id)
        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        # Odoo marks it paid (or in_payment until the bank statement) on its own.
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertAlmostEqual(invoice.amount_residual, 0.0, 2)

    def test_payment_without_invoice_is_standalone(self):
        # No invoice on the order -> a standalone customer payment is posted.
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        payment._apply_intent_status({
            "id": "pi_test_123",
            "status": "succeeded",
            "latest_charge": "ch_test_1",
        })
        self.assertEqual(payment.status, "succeeded")
        self.assertFalse(payment.account_payment_id.reconciled_invoice_ids)
        self.assertEqual(payment.account_payment_id.move_id.state, "posted")

    def test_card_surcharge_uses_one_gross_payment_without_customer_credit(self):
        self._enable_fees(var=0.029, fixed=0.30)
        invoice = self._post_invoice()
        payment = self._make_payment(amount=invoice.amount_total)
        payment_count = self.env["account.payment"].search_count([])
        with self._patch_stripe(self._build_mock_stripe(
            retrieve_status="succeeded",
            collect_before_confirm=True,
            funding="credit",
        )) as m:
            payment._process_on_reader()

        # A non-zero fee was computed and stored.
        self.assertGreater(payment.fee_amount, 0.0)
        self.assertAlmostEqual(
            payment.total_charged, payment.amount + payment.fee_amount, 2)

        # Stripe collects the card at the base amount, then updates to amount +
        # surcharge only after Stripe identifies the card as credit.
        create_kwargs = m.PaymentIntent.create.call_args.kwargs
        self.assertEqual(
            create_kwargs["amount"],
            stripe_client.to_stripe_amount(payment.amount, payment.currency_id),
        )
        expected_minor = stripe_client.to_stripe_amount(
            payment.amount + payment.fee_amount, payment.currency_id)
        self.assertEqual(
            m.PaymentIntent.modify.call_args.kwargs["amount"],
            expected_minor,
        )
        m.terminal.Reader.collect_payment_method.assert_called_once()
        m.terminal.Reader.confirm_payment_intent.assert_called_once()

        # One gross payment settles both the sale invoice and surcharge invoice.
        self.assertTrue(payment.account_payment_id)
        self.assertFalse(payment.fee_account_payment_id)
        self.assertTrue(payment.fee_move_id)
        self.assertEqual(payment.fee_move_id.state, "posted")
        self.assertEqual(
            self.env["account.payment"].search_count([]),
            payment_count + 1,
        )
        self.assertAlmostEqual(
            payment.account_payment_id.amount,
            payment.total_charged,
            2,
        )
        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        payment.fee_move_id.invalidate_recordset(
            ["payment_state", "amount_residual"]
        )
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertIn(payment.fee_move_id.payment_state, ("paid", "in_payment"))
        self.assertEqual(invoice.amount_residual, 0.0)
        self.assertEqual(payment.fee_move_id.amount_residual, 0.0)

    def test_card_fee_is_flat_percentage_not_grossed_up(self):
        """A 3% surcharge on $100 must be $3.00 (flat), not $3.09 (grossed up)."""
        self._enable_fees(var=0.03, fixed=0.0)
        provider = self.env["payment.provider"].sudo().search(
            [("code", "=", "stripe")], limit=1)
        country = provider.company_id.country_id
        fee = provider._card_compute_fees(100.0, provider.main_currency_id, country)
        self.assertAlmostEqual(fee, 3.00, 2,
                               "3%% of $100 should be a flat $3.00, got %s" % fee)

    def test_card_prepayment_adds_surcharge_order_line_and_one_gross_credit(self):
        self._enable_fees(var=0.03, fixed=0.0)
        payment = self._make_payment(amount=100.0)
        original_total = self.sale_order.amount_total

        with self._patch_stripe(self._build_mock_stripe(
            retrieve_status="succeeded",
            collect_before_confirm=True,
            funding="credit",
        )):
            payment._process_on_reader()

        self.assertTrue(payment.surcharge_sale_order_line_id)
        self.assertFalse(payment.fee_move_id)
        self.assertFalse(payment.fee_account_payment_id)
        self.assertAlmostEqual(payment.fee_amount, 3.0, 2)
        self.assertAlmostEqual(
            self.sale_order.amount_total,
            original_total + payment.fee_amount,
            2,
        )
        self.assertAlmostEqual(
            payment.account_payment_id.amount,
            payment.total_charged,
            2,
        )
        self.assertFalse(payment.account_payment_id.reconciled_invoice_ids)

    def test_terminal_debit_card_does_not_receive_surcharge(self):
        self._enable_fees(var=0.03, fixed=0.0)
        payment = self._make_payment(amount=100.0)

        with self._patch_stripe(self._build_mock_stripe(
            retrieve_status="succeeded",
            collect_before_confirm=True,
            funding="debit",
        )) as stripe:
            payment._process_on_reader()

        self.assertEqual(payment.stripe_card_funding, "debit")
        self.assertEqual(payment.fee_amount, 0.0)
        self.assertFalse(payment.fee_move_id)
        self.assertFalse(payment.surcharge_sale_order_line_id)
        self.assertEqual(
            stripe.PaymentIntent.modify.call_args.kwargs["amount"],
            stripe_client.to_stripe_amount(100.0, payment.currency_id),
        )
        self.assertEqual(payment.account_payment_id.amount, 100.0)

    def test_terminal_surcharge_rejects_mixed_invoice_tax_contexts(self):
        self._enable_fees(var=0.03, fixed=0.0)
        invoice = self._post_invoice()
        alternate_fiscal_position = self.env[
            "account.fiscal.position"
        ].create({
            "name": "Alternate Terminal Tax Treatment",
            "company_id": self.company.id,
        })
        second_invoice = self.init_invoice(
            "out_invoice",
            self.partner,
            amounts=[50.0],
        )
        second_invoice.fiscal_position_id = alternate_fiscal_position
        second_invoice.action_post()
        payment = self._make_payment(amount=invoice.amount_total)
        payment.stripe_card_funding = "credit"

        with (
            patch.object(
                type(self.sale_order),
                "_stripe_terminal_open_invoices",
                return_value=invoice | second_invoice,
            ),
            self.assertRaisesRegex(
                ValidationError,
                "different tax treatments",
            ),
        ):
            payment._prepare_terminal_surcharge(self.provider)

        self.assertFalse(payment.fee_move_id)
        self.assertFalse(payment.surcharge_sale_order_line_id)

    def test_partial_refund_returns_proportional_surcharge(self):
        self._enable_fees(var=0.03, fixed=0.0)
        invoice = self._post_invoice()
        payment = self._make_payment(amount=invoice.amount_total)
        with self._patch_stripe(self._build_mock_stripe(
            retrieve_status="succeeded",
            collect_before_confirm=True,
            funding="credit",
        )):
            payment._process_on_reader()

        with self._patch_stripe(self._build_mock_stripe(
            refund_amount=5150,
        )) as stripe:
            payment._process_refund(50.0)

        self.assertEqual(payment.status, "succeeded")
        self.assertAlmostEqual(payment.refunded_amount, 50.0, 2)
        self.assertAlmostEqual(payment.refunded_surcharge_amount, 1.5, 2)
        self.assertEqual(len(payment.surcharge_refund_move_ids), 1)
        credit_note = payment.surcharge_refund_move_ids
        self.assertEqual(credit_note.state, "posted")
        self.assertEqual(credit_note.move_type, "out_refund")
        stripe.Refund.create.assert_called_once_with(
            payment_intent=payment.payment_intent_id,
            amount=5150,
            idempotency_key=f"terminal_refund_{payment.id}_5000",
        )
        invoice.invalidate_recordset(["amount_residual", "payment_state"])
        self.assertAlmostEqual(invoice.amount_residual, 50.0, 2)

    def test_card_prepayment_reconciles_when_order_is_invoiced(self):
        self._enable_fees(var=0.03, fixed=0.0)
        self.product.product_tmpl_id.invoice_policy = "order"
        payment = self._make_payment(amount=self.sale_order.amount_total)
        with self._patch_stripe(self._build_mock_stripe(
            retrieve_status="succeeded",
            collect_before_confirm=True,
            funding="credit",
        )):
            payment._process_on_reader()

        invoice = self.sale_order._create_invoices()
        invoice.action_post()

        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        self.assertAlmostEqual(
            invoice.amount_total,
            payment.total_charged,
            2,
        )
        self.assertEqual(invoice.amount_residual, 0.0)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertIn(invoice, payment.account_payment_id.reconciled_invoice_ids)

    def test_no_fee_when_provider_fees_inactive(self):
        invoice = self._post_invoice()
        payment = self._make_payment(amount=invoice.amount_total)
        with self._patch_stripe(self._build_mock_stripe(retrieve_status="succeeded")):
            payment._process_on_reader()
        self.assertEqual(payment.fee_amount, 0.0)
        self.assertFalse(payment.fee_account_payment_id)

    def test_cash_collection_is_plain_account_payment(self):
        # Cash, no invoice -> immediate standalone account.payment, no Stripe,
        # and NO stripe.terminal.payment record.
        payment = self._collect_cash(40.0)
        self.assertEqual(payment._name, "account.payment")
        self.assertEqual(payment.payment_type, "inbound")
        self.assertEqual(payment.sale_order_id, self.sale_order)
        self.assertEqual(payment.move_id.state, "posted")
        self.assertFalse(payment.reconciled_invoice_ids)
        self.assertEqual(self.env["stripe.terminal.payment"].search_count(
            [("sale_order_id", "=", self.sale_order.id)]), 0)

    def test_cash_uses_the_configured_journal_and_payment_method(self):
        """The fixture points Cash Journal at the card journal, so cash lands
        there too and only the Cash Payment Method tells the two apart."""
        payment = self._collect_cash(40.0)
        self.assertEqual(payment.journal_id, self.provider.journal_id)
        self.assertEqual(payment.payment_method_line_id, self.cash_method_line)

    def test_cash_can_use_a_journal_of_its_own(self):
        """Cash Journal is free to differ from the card journal."""
        cash_journal = self.env["account.journal"].create({
            "name": "Counter Cash", "type": "cash", "code": "UCASH",
            "company_id": self.company.id,
        })
        self.provider.write({
            "unico_cash_journal_id": cash_journal.id,
            "unico_cash_payment_method_line_id": False,
        })
        payment = self._collect_cash(40.0)
        self.assertEqual(payment.journal_id, cash_journal)
        # No Cash Payment Method set -> the journal's own default is used.
        self.assertEqual(payment.payment_method_line_id.journal_id, cash_journal)

    def test_cash_without_configuration_falls_back_to_the_card_journal(self):
        """An unconfigured provider still collects: cash goes where cards go."""
        self.provider.write({
            "unico_cash_payment_method_line_id": False,
            "unico_cash_journal_id": False,
        })
        payment = self._collect_cash(40.0)
        self.assertEqual(payment.journal_id, self.provider.journal_id)
        self.assertTrue(payment.payment_method_line_id)

    def test_cash_payment_method_must_belong_to_the_cash_journal(self):
        other_journal = self.env["account.journal"].create({
            "name": "Other Bank", "type": "bank", "code": "UOTHR",
            "company_id": self.company.id,
        })
        with self.assertRaises(ValidationError):
            self.provider.write({"unico_cash_journal_id": other_journal.id})

    def test_cash_collection_reconciles_invoice(self):
        invoice = self._post_invoice()
        payment = self._collect_cash(invoice.amount_total)
        self.assertEqual(payment.journal_id, self.provider.journal_id)
        self.assertEqual(payment.payment_method_line_id, self.cash_method_line)
        self.assertEqual(payment.sale_order_id, self.sale_order)
        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))

    def test_poll_cron_advances_processing_payments(self):
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_test_123"})
        with self._patch_stripe(self._build_mock_stripe(retrieve_status="succeeded")):
            self.env["stripe.terminal.payment"]._cron_poll_processing()
        self.assertEqual(payment.status, "succeeded")

    def test_poll_cron_marks_missing_payment_intent_failed(self):
        payment = self._make_payment()
        payment.write({"status": "processing", "payment_intent_id": "pi_missing"})
        mock = self._build_mock_stripe()
        mock.PaymentIntent.retrieve.side_effect = Exception(
            "No such payment_intent: 'pi_missing'")

        with self._patch_stripe(mock):
            self.env["stripe.terminal.payment"]._cron_poll_processing()

        self.assertEqual(payment.status, "failed")
        self.assertIn("No such payment_intent", payment.error_message)
