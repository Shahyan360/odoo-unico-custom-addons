from odoo.tests import tagged

from .common import StripeTerminalCommon


@tagged("post_install", "-at_install")
class TestUnicoPaymentStatus(StripeTerminalCommon):
    """The Paid ribbon / Amount Due / Payments smart button must reflect every
    way a customer can settle an order: terminal & cash (linked by
    sale_order_id), and manual account.payment / online-portal payments that
    only reconcile against the invoice."""

    def setUp(self):
        super().setUp()
        if "unico_payment_status" not in self.env["sale.order"]._fields:
            self.skipTest("unico_payment_status is provided by unico_delivery")

    def _register_invoice_payment(self, invoice, amount=None):
        """Pay an invoice the standard way (no sale_order_id), mirroring a manual
        Register Payment and an online/portal payment."""
        register = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids,
        ).create({"journal_id": self.journal.id, **({"amount": amount} if amount else {})})
        return register._create_payments()

    def test_invoice_payment_marks_order_paid(self):
        invoice = self._post_invoice()
        order = self.sale_order
        self.assertEqual(order.unico_payment_status, "not_paid")

        payment = self._register_invoice_payment(invoice)
        # The payment is NOT tagged to the order — it only reconciles the invoice.
        self.assertFalse(payment.sale_order_id)

        order.invalidate_recordset()
        self.assertEqual(order.unico_payment_status, "paid")
        self.assertEqual(order.stripe_terminal_payment_count, 1)
        self.assertEqual(
            order.currency_id.compare_amounts(order.stripe_terminal_amount_due, 0), 0)

        # The smart button surfaces the invoice payment even without a link.
        action = order.action_view_stripe_terminal_payments()
        self.assertEqual(action["res_model"], "account.payment")
        self.assertIn(payment.id, action["domain"][0][2])

    def test_partial_invoice_payment_marks_paid(self):
        """Any collected payment marks the order Paid, even a partial one: the
        Paid status follows the presence of a payment (like the count), not full
        coverage. The amount still due remains positive."""
        invoice = self._post_invoice()
        order = self.sale_order
        half = order.amount_total / 2.0

        self._register_invoice_payment(invoice, amount=half)
        order.invalidate_recordset()

        self.assertEqual(order.unico_payment_status, "paid")
        self.assertGreater(order.stripe_terminal_amount_due, 0.0)
        self.assertEqual(order.stripe_terminal_payment_count, 1)

    def test_terminal_cash_still_marks_paid(self):
        """Regression: the original terminal/cash flow keeps working."""
        order = self.sale_order
        self._collect_cash(order.amount_total)

        order.invalidate_recordset()
        self.assertEqual(order.unico_payment_status, "paid")
        self.assertEqual(order.stripe_terminal_payment_count, 1)

    def test_terminal_and_invoice_payment_not_double_counted(self):
        """A terminal payment reconciled against the invoice appears in both the
        sale_order_id link and the invoice reconciliation, but must be counted
        once."""
        invoice = self._post_invoice()
        order = self.sale_order
        # Register against the invoice, then tag with the order as the terminal
        # collect flow does — the same payment is now reachable both ways.
        payment = self._register_invoice_payment(invoice)
        payment.sale_order_id = order.id

        order.invalidate_recordset()
        self.assertEqual(order.stripe_terminal_payment_count, 1)
        self.assertEqual(order.unico_payment_status, "paid")
