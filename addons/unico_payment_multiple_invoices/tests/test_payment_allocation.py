"""Unallocated amount on a payment.

Odoo only exposes a yes/no ``is_reconciled`` flag, so a customer who paid 500
with 300 applied to an invoice looks exactly like one whose payment is
untouched. The real figure lives on the payment's receivable (or payable)
journal item, as that line's residual.
"""

from odoo import Command
from odoo.tests import tagged
from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUnicoPaymentAllocation(AccountTestInvoicingCommon):

    def _make_payment(self, amount, payment_type="inbound", partner_type="customer"):
        payment = self.env["account.payment"].create({
            "amount": amount,
            "payment_type": payment_type,
            "partner_type": partner_type,
            "partner_id": self.partner_a.id,
            "date": "2026-01-10",
        })
        payment.action_post()
        return payment

    def _make_invoice(self, amount, move_type="out_invoice"):
        invoice = self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": self.partner_a.id,
            "invoice_date": "2026-01-05",
            "date": "2026-01-05",
            "invoice_line_ids": [Command.create({
                "name": "Line",
                "quantity": 1,
                "price_unit": amount,
                "tax_ids": [],
            })],
        })
        invoice.action_post()
        return invoice

    def _reconcile(self, payment, invoice):
        account = payment.move_id.line_ids.filtered(
            lambda line: line.account_id.account_type in (
                "asset_receivable", "liability_payable",
            )
        ).account_id
        lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
            lambda line: line.account_id == account and not line.reconciled
        )
        lines.reconcile()

    # ------------------------------------------------------------------
    def test_untouched_payment_is_fully_unallocated(self):
        payment = self._make_payment(500.0)

        self.assertEqual(payment.unico_amount_unallocated, 500.0)

    def test_partly_applied_customer_payment(self):
        """Paid 500, 300 goes to an invoice, 200 stays free."""
        payment = self._make_payment(500.0)
        invoice = self._make_invoice(300.0)

        self._reconcile(payment, invoice)

        self.assertEqual(payment.unico_amount_unallocated, 200.0)

    def test_fully_applied_customer_payment(self):
        payment = self._make_payment(500.0)
        invoice = self._make_invoice(500.0)

        self._reconcile(payment, invoice)

        self.assertEqual(payment.unico_amount_unallocated, 0.0)
        self.assertTrue(payment.is_reconciled)

    def test_vendor_payment_reads_the_same_way(self):
        """The payable line is a debit rather than a credit, so the raw
        residual carries the opposite sign; the figure must still be positive."""
        payment = self._make_payment(
            500.0, payment_type="outbound", partner_type="supplier",
        )
        bill = self._make_invoice(300.0, move_type="in_invoice")

        self._reconcile(payment, bill)

        self.assertEqual(payment.unico_amount_unallocated, 200.0)

    def test_draft_payment_shows_nothing(self):
        payment = self.env["account.payment"].create({
            "amount": 500.0,
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": self.partner_a.id,
        })

        self.assertEqual(payment.state, "draft")
        self.assertEqual(payment.unico_amount_unallocated, 0.0)

    def test_cancelled_payment_is_not_reported_as_free_money(self):
        payment = self._make_payment(500.0)
        payment.action_cancel()

        self.assertEqual(payment.unico_amount_unallocated, 0.0)

    def test_unallocated_payments_are_searchable(self):
        partly = self._make_payment(500.0)
        self._reconcile(partly, self._make_invoice(300.0))
        fully = self._make_payment(400.0)
        self._reconcile(fully, self._make_invoice(400.0))

        found = self.env["account.payment"].search([
            ("id", "in", (partly | fully).ids),
            ("unico_amount_unallocated", ">", 0),
        ])

        self.assertEqual(found, partly)
