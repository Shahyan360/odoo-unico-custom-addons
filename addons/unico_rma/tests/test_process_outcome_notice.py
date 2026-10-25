"""Tests for the "what will this RMA create" notice above the lines table.

Which documents an RMA produces is decided per line by the resolution's
action, which the lines table does not spell out. The notice says it in one
sentence — and must stay in step with what process_rma actually branches on.
"""
from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestRmaProcessOutcomeNotice(RmaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_refund_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund With Return",
            "rma_action": "refund_with_returned_item",
        })
        cls.resolution_return_with_credit = cls.env["rma.resolution"].create({
            "rma_resolution": "Return With Credit",
            "rma_action": "return_with_credit",
        })

    def _rma_with(self, resolution, return_qty=2, **overrides):
        return self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": return_qty,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "rma_resolution_id": resolution.id,
                "return_reason_id": self.return_reason.id,
                "sale_line_id": self.sale_order.order_line[0].id,
            }],
            **overrides,
        )

    # -- Credit note only, transfer only, both, neither --------------------

    def test_refund_credits_without_taking_goods_back(self):
        outcomes = self._rma_with(self.resolution_refund)._rma_processing_outcomes()
        self.assertIn("a customer credit note", outcomes)
        self.assertNotIn("an incoming return transfer", outcomes)

    def test_refund_with_return_creates_both(self):
        outcomes = self._rma_with(
            self.resolution_refund_with_return)._rma_processing_outcomes()
        self.assertIn("an incoming return transfer", outcomes)
        self.assertIn("a customer credit note", outcomes)

    def test_return_with_credit_takes_goods_back_without_a_credit_note(self):
        """It frees the collected payment instead of raising a credit note."""
        outcomes = self._rma_with(
            self.resolution_return_with_credit)._rma_processing_outcomes()
        self.assertIn("an incoming return transfer", outcomes)
        self.assertIn("a release of the payment already collected", outcomes)
        self.assertNotIn("a customer credit note", outcomes)

    def test_plain_replacement_creates_neither(self):
        rma = self._rma_with(self.resolution_replacement)
        self.assertEqual(rma._rma_processing_outcomes(), [])
        self.assertIn("neither", rma.process_outcome_notice)

    def test_zero_return_qty_creates_neither(self):
        rma = self._rma_with(self.resolution_refund_with_return, return_qty=0)
        self.assertEqual(rma._rma_processing_outcomes(), [])

    def test_replacement_products_add_a_sales_order(self):
        rma = self._rma_with(
            self.resolution_replacement,
            replacements=[{"product_id": self.product_b.id, "qty": 1}],
        )
        self.assertEqual(
            rma._rma_processing_outcomes(), ["a replacement sales order"])

    # -- Sage returns differ ----------------------------------------------

    def test_sage_return_with_credit_is_not_offered(self):
        """A Sage return only receives goods back on refund-with-return, and
        never releases a collected Odoo payment."""
        rma = self._make_rma(
            rma_type="legacy_sage",
            sale_order=False,
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 2,
                "price_unit": 100.0,
                "rma_resolution_id": self.resolution_return_with_credit.id,
                "return_reason_id": self.return_reason.id,
            }],
        )
        self.assertEqual(rma._rma_processing_outcomes(), [])

    # -- The rendered sentence --------------------------------------------

    def test_notice_lists_every_outcome(self):
        notice = self._rma_with(
            self.resolution_refund_with_return).process_outcome_notice
        self.assertTrue(notice.startswith("Processing this RMA will create:"))
        self.assertIn("an incoming return transfer", notice)
        self.assertIn("a customer credit note", notice)

    def test_notice_follows_a_resolution_change(self):
        rma = self._rma_with(self.resolution_refund)
        self.assertNotIn("return transfer", rma.process_outcome_notice)
        rma.rma_line_ids.rma_resolution_id = self.resolution_refund_with_return
        self.assertIn("return transfer", rma.process_outcome_notice)
