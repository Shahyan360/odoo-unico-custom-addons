from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestReturnedCreditedStatus(AccountTestInvoicingCommon):
    """``unico_payment_status`` gains a Returned/Credited state for orders whose
    own sale was returned and fully credited back through an RMA credit note,
    and marks an order Paid when its invoices are fully covered by an applied
    credit note (no collected payment). Detection keys off the credit note, so
    it is independent of how the order was originally paid."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.ref("sales_team.group_sale_manager").sudo().write(
            {"user_ids": [(4, cls.env.user.id)]})
        cls.product = cls.product_a
        cls.product.product_tmpl_id.invoice_policy = "order"

    # ------------------------------------------------------------------
    def _order(self, price=100.0):
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
                "price_unit": price,
                "tax_ids": [(6, 0, [])],
            })],
        })
        order.action_confirm()
        return order

    def _post_invoice(self, order):
        invoice = order._create_invoices()
        invoice.action_post()
        return invoice

    def _post_rma_credit_note(self, order, amount, with_rma=True):
        rma = False
        if with_rma:
            rma = self.env["rma.main"].create({"sale_order": order.id})
        move = self.env["account.move"].create({
            "move_type": "out_refund",
            "partner_id": order.partner_id.id,
            "sale_id": order.id,
            "rma_id": rma.id if rma else False,
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id,
                "quantity": 1.0,
                "price_unit": amount,
                "tax_ids": [(6, 0, [])],
            })],
        })
        move.action_post()
        return move

    # ------------------------------------------------------------------
    def test_full_rma_credit_marks_returned_credited(self):
        """A fully-credited RMA return flips the order to Returned/Credited,
        taking precedence over any Paid state."""
        order = self._order(price=100.0)
        self._post_invoice(order)
        self._post_rma_credit_note(order, amount=100.0)

        order.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(order.unico_payment_status, "returned_credited")

    def test_partial_rma_credit_does_not_flag_returned_credited(self):
        """A partial RMA credit does not claim the order was returned; with no
        collected payment and an unsettled invoice it stays Unpaid."""
        order = self._order(price=100.0)
        self._post_invoice(order)
        self._post_rma_credit_note(order, amount=40.0)

        order.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(order.unico_payment_status, "not_paid")

    def test_invoice_fully_covered_by_credit_marks_paid(self):
        """An order with no collected payment whose invoice is fully reconciled
        against a credit note reads as Paid (fully covered)."""
        order = self._order(price=100.0)
        invoice = self._post_invoice(order)
        # Credit note unrelated to an RMA (no rma_id) so it does not trip the
        # Returned/Credited branch — it just settles the invoice.
        credit = self._post_rma_credit_note(order, amount=100.0, with_rma=False)
        (invoice + credit).line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        ).reconcile()
        # A full credit-note offset settles the invoice as 'reversed' (not
        # 'paid', which is for a collected payment) — both mean no residual.
        self.assertEqual(invoice.payment_state, "reversed")

        order.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(order.unico_payment_status, "paid")

    def test_invoice_partially_covered_by_credit_stays_unpaid(self):
        """Partial credit coverage (no payment) leaves the order Unpaid: the
        credit-settlement branch counts only when fully covered."""
        order = self._order(price=100.0)
        invoice = self._post_invoice(order)
        credit = self._post_rma_credit_note(order, amount=40.0, with_rma=False)
        (invoice + credit).line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        ).reconcile()
        self.assertEqual(invoice.payment_state, "partial")

        order.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(order.unico_payment_status, "not_paid")
