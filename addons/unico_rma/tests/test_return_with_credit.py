"""Tests for the "return_with_credit" RMA resolution.

Unlike a refund, this action receives the goods back (a return picking is
created, like the ``*_with_returned_item`` actions) but instead of issuing
a credit note it releases the customer's already-collected payment so it
can be re-applied to a new invoice.

The heavy accounting internals (reconcile/unreconcile, sale-order cancel)
are core Odoo and are exercised elsewhere; these tests focus on the new
dispatch and release/cancel decision logic.
"""
from unittest.mock import patch

from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestReturnWithCreditDispatch(RmaTestCommon):
    """process_rma must route return_with_credit to returns + release,
    never to the credit-note path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_return_with_credit = cls.env["rma.resolution"].create({
            "rma_resolution": "Return With Credit",
            "rma_action": "return_with_credit",
        })

    def _make_rma_rwc(self, return_qty=2):
        return self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": return_qty,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "sale_line_id": self.sale_order.order_line[0].id,
                "rma_resolution_id": self.resolution_return_with_credit.id,
                "return_reason_id": self.return_reason.id,
            }],
        )

    def test_dispatch_routes_to_returns_and_release_not_credit_note(self):
        rma = self._make_rma_rwc()
        cls = type(rma)
        with patch.object(cls, "validate_invoice", return_value=None), \
             patch.object(cls, "process_credit_note", return_value=None) as mock_cn, \
             patch.object(cls, "process_returned_items", return_value=None) as mock_ret, \
             patch.object(cls, "process_replacement_items", return_value=None) as mock_rep, \
             patch.object(cls, "_process_return_with_credit", return_value=None) as mock_rwc:
            rma.process_rma()
            mock_cn.assert_not_called()
            mock_rep.assert_not_called()
            mock_ret.assert_called_once()
            mock_rwc.assert_called_once()
            (ret_lines,) = mock_ret.call_args.args
            (rwc_lines,) = mock_rwc.call_args.args
            self.assertEqual(ret_lines, rma.rma_line_ids)
            self.assertEqual(rwc_lines, rma.rma_line_ids)


@tagged("post_install", "-at_install")
class TestReturnWithCreditRelease(RmaTestCommon):
    """Value computation and the full-vs-partial release decision."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_return_with_credit = cls.env["rma.resolution"].create({
            "rma_resolution": "Return With Credit",
            "rma_action": "return_with_credit",
        })
        cls.bank_journal = cls.env["account.journal"].search(
            [("type", "in", ("bank", "cash")), ("company_id", "=", cls.env.company.id)],
            limit=1,
        )

    def _rma(self, return_qty):
        return self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": return_qty,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "sale_line_id": self.sale_order.order_line[0].id,
                "rma_resolution_id": self.resolution_return_with_credit.id,
                "return_reason_id": self.return_reason.id,
            }],
        )

    def _collected_payment(self, amount):
        if not self.bank_journal:
            self.skipTest("No bank/cash journal available in this test database.")
        payment = self.env["account.payment"].create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": self.partner.id,
            "amount": amount,
            "journal_id": self.bank_journal.id,
        })
        payment.action_post()
        payment.sale_order_id = self.sale_order.id
        return payment

    def test_value_uses_tax_included_sale_line_total(self):
        rma = self._rma(2)
        # Sale line: 5 x 100 = 500 total; per unit 100; 2 returned -> 200.
        self.assertEqual(
            rma._return_with_credit_value(rma.rma_line_ids),
            self.sale_order.order_line[0].price_total / 5.0 * 2.0,
        )

    def test_full_return_frees_payment_and_cancels_order(self):
        payment = self._collected_payment(self.sale_order.amount_total)
        rma = self._rma(5)  # whole order returned
        with patch.object(type(rma), "_cancel_sale_and_invoices") as mock_cancel, \
             patch.object(
                 type(self.sale_order),
                 "_unico_customer_payments_map",
                 return_value={self.sale_order.id: payment},
             ):
            rma._process_return_with_credit(rma.rma_line_ids)
            mock_cancel.assert_called_once_with(self.sale_order)
        # Payment detached from the sale order so it is free to re-apply.
        self.assertFalse(payment.sale_order_id)
        # Released payment tracked, and its full (unreconciled) amount shows
        # as available credit for the banner.
        self.assertEqual(rma.credit_released_payment_ids, payment)
        self.assertEqual(rma.credit_available_amount, payment.amount)

    def test_partial_return_frees_payment_without_cancelling(self):
        payment = self._collected_payment(self.sale_order.amount_total)
        rma = self._rma(2)  # only part of the order returned
        with patch.object(type(rma), "_cancel_sale_and_invoices") as mock_cancel, \
             patch.object(
                 type(self.sale_order),
                 "_unico_customer_payments_map",
                 return_value={self.sale_order.id: payment},
             ):
            rma._process_return_with_credit(rma.rma_line_ids)
            mock_cancel.assert_not_called()
        self.assertFalse(payment.sale_order_id)

    def test_no_collected_payment_is_a_noop(self):
        rma = self._rma(5)
        empty = self.env["account.payment"]
        with patch.object(type(rma), "_cancel_sale_and_invoices") as mock_cancel, \
             patch.object(
                 type(self.sale_order),
                 "_unico_customer_payments_map",
                 return_value={self.sale_order.id: empty},
             ):
            # Should not raise and must not cancel anything.
            rma._process_return_with_credit(rma.rma_line_ids)
            mock_cancel.assert_not_called()
