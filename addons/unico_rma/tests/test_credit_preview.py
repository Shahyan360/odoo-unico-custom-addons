"""Tests for the approval-stage credit preview.

The preview itemises subtotal / return fee / tax / net credit on the RMA
form so an approver is not judging the RMA on a bare untaxed subtotal.
It must agree with what process_credit_note actually posts — these tests
pin that agreement.
"""
from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestRmaCreditPreview(RmaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tax_10 = cls.env["account.tax"].create({
            "name": "RMA Test Tax 10%",
            "amount": 10.0,
            "amount_type": "percent",
            "type_tax_use": "sale",
        })
        cls.resolution_fee_pct = cls.env["rma.resolution"].create({
            "rma_resolution": "Wrong Item",
            "rma_action": "refund",
            "apply_return_fee": True,
            "return_fee_type": "percentage",
            "return_fee_value": 10.0,
        })
        cls.resolution_fee_fixed = cls.env["rma.resolution"].create({
            "rma_resolution": "Damaged",
            "rma_action": "refund",
            "apply_return_fee": True,
            "return_fee_type": "amount",
            "return_fee_value": 25.0,
        })

    def _taxed_sale_order(self):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {
                "product_id": self.product_a.id,
                "product_uom_qty": 5.0,
                "price_unit": 100.0,
                "tax_ids": [(6, 0, self.tax_10.ids)],
            })],
        })

    def _untaxed_sale_order(self):
        """A sale order line picks up the company's default sale tax unless it
        is explicitly cleared, so the no-tax case has to say so rather than
        just omitting tax_ids."""
        order = self._taxed_sale_order()
        order.order_line.tax_ids = [(6, 0, [])]
        return order

    def _rma_with_line(self, resolution, order=None, **line_overrides):
        order = order or self._taxed_sale_order()
        line = {
            "product_id": self.product_a.id,
            "return_qty": 2,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "rma_resolution_id": resolution.id,
            "return_reason_id": self.return_reason.id,
            "sale_line_id": order.order_line[0].id,
        }
        line.update(line_overrides)
        return self._make_rma(rma_type="rma_with_do", sale_order=order.id, lines=[line])

    # -- Tax ---------------------------------------------------------------

    def test_tax_is_shown_from_the_sale_order_line(self):
        """200.00 returned at 10% tax -> 20.00 tax, 220.00 credited."""
        rma = self._rma_with_line(self.resolution_refund)
        self.assertAlmostEqual(rma.credit_preview_untaxed, 200.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_tax, 20.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_total, 220.0, places=2)

    def test_untaxed_sale_line_shows_no_tax(self):
        rma = self._rma_with_line(
            self.resolution_refund, order=self._untaxed_sale_order()
        )
        self.assertAlmostEqual(rma.credit_preview_tax, 0.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_total, 200.0, places=2)

    def test_discount_reduces_the_taxable_base(self):
        """The credit note copies the sale line's discount, so the preview must
        tax the discounted price rather than the list price."""
        order = self._taxed_sale_order()
        order.order_line[0].discount = 50.0
        rma = self._rma_with_line(self.resolution_refund, order=order)
        self.assertAlmostEqual(rma.credit_preview_untaxed, 100.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_tax, 10.0, places=2)

    # -- Return fee --------------------------------------------------------

    def test_percentage_fee_is_itemised_and_deducted(self):
        """200.00 less a 10% fee -> 180.00 subtotal; tax stays on the full 200."""
        rma = self._rma_with_line(self.resolution_fee_pct)
        self.assertAlmostEqual(rma.credit_preview_fee, -20.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_untaxed, 180.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_tax, 20.0, places=2)
        self.assertAlmostEqual(rma.credit_preview_total, 200.0, places=2)

    def test_fixed_fee_is_charged_once_for_the_resolution(self):
        """A fixed fee is per-resolution, not per unit — 2 units, one $25 fee."""
        rma = self._rma_with_line(self.resolution_fee_fixed)
        self.assertAlmostEqual(rma.credit_preview_fee, -25.0, places=2)

    def test_fee_label_describes_the_basis(self):
        rma = self._rma_with_line(self.resolution_fee_pct)
        self.assertEqual(rma.credit_preview_fee_label, "Wrong Item 10%")

    def test_no_fee_when_resolution_does_not_apply_one(self):
        rma = self._rma_with_line(self.resolution_refund)
        self.assertAlmostEqual(rma.credit_preview_fee, 0.0, places=2)
        self.assertFalse(rma.credit_preview_fee_label)

    def test_has_fee_flag_tells_the_two_cases_apart(self):
        """The footer states "no restocking fee" rather than hiding the row, so
        an approver never has to read a blank as an answer."""
        self.assertTrue(
            self._rma_with_line(self.resolution_fee_pct).credit_preview_has_fee)
        self.assertFalse(
            self._rma_with_line(self.resolution_refund).credit_preview_has_fee)

    def test_has_fee_flag_survives_the_posted_credit_note(self):
        rma = self._rma_with_line(self.resolution_fee_pct)
        rma.process_credit_note(rma.rma_line_ids)
        rma.invalidate_recordset()
        self.assertFalse(rma.credit_preview_is_estimate)
        self.assertTrue(rma.credit_preview_has_fee)

    # -- Agreement with the posted credit note -----------------------------

    def test_preview_matches_the_credit_note_it_produces(self):
        """The whole point: what the approver sees is what gets posted."""
        rma = self._rma_with_line(self.resolution_fee_pct)
        preview_untaxed = rma.credit_preview_untaxed
        preview_tax = rma.credit_preview_tax
        preview_total = rma.credit_preview_total

        rma.process_credit_note(rma.rma_line_ids)
        credit_note = self.env["account.move"].search([("rma_id", "=", rma.id)])
        self.assertEqual(len(credit_note), 1)

        self.assertAlmostEqual(credit_note.amount_untaxed, preview_untaxed, places=2)
        self.assertAlmostEqual(credit_note.amount_tax, preview_tax, places=2)
        self.assertAlmostEqual(credit_note.amount_total, preview_total, places=2)

    def test_preview_reads_the_credit_note_once_it_exists(self):
        """After the credit note is created the preview stops estimating and
        reports the real document, so the two can never disagree on screen."""
        rma = self._rma_with_line(self.resolution_fee_pct)
        self.assertTrue(rma.credit_preview_is_estimate)

        rma.process_credit_note(rma.rma_line_ids)
        rma.invalidate_recordset()
        self.assertFalse(rma.credit_preview_is_estimate)

        credit_note = self.env["account.move"].search([("rma_id", "=", rma.id)])
        self.assertAlmostEqual(
            rma.credit_preview_total, credit_note.amount_total, places=2
        )
        self.assertAlmostEqual(rma.credit_preview_fee, -20.0, places=2)

    # -- Which lines count -------------------------------------------------

    def test_replacement_only_line_is_not_credited(self):
        """A plain replacement produces no credit note, so nothing to preview."""
        rma = self._rma_with_line(self.resolution_replacement)
        self.assertFalse(rma._credit_note_items())
        self.assertAlmostEqual(rma.credit_preview_total, 0.0, places=2)

    def test_legacy_sage_defaults_to_no_tax(self):
        """Sage returns post untaxed unless Tax Handling says otherwise, and the
        preview must show that rather than implying tax will be added."""
        rma = self._make_rma(
            rma_type="legacy_sage",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 2,
                "price_unit": 100.0,
                "rma_resolution_id": self.resolution_refund.id,
                "return_reason_id": self.return_reason.id,
                "legacy_tax_ids": [(6, 0, self.tax_10.ids)],
            }],
        )
        self.assertEqual(rma.legacy_tax_treatment, "no_odoo_tax")
        self.assertAlmostEqual(rma.credit_preview_tax, 0.0, places=2)

        rma.legacy_tax_treatment = "selected_odoo_tax"
        self.assertAlmostEqual(rma.credit_preview_tax, 20.0, places=2)
