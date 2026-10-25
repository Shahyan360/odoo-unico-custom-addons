"""Tests for the 4 RMA resolution flows and state transitions.

Uses mock.patch to isolate process_rma's dispatch logic from the heavy
sub-methods (which require posted invoices, real pickings, etc.).
"""
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestProcessRmaDispatch(RmaTestCommon):
    """Verify that process_rma routes each of the 4 resolutions correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # One resolution per action so each test can pick the right one
        cls.resolution_refund_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund With Return",
            "rma_action": "refund_with_returned_item",
        })
        cls.resolution_replacement_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Replacement With Return",
            "rma_action": "replacement_with_returned_item",
        })

    def _make_rma_with_action(self, resolution, replacements=None):
        rma = self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 2,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "rma_resolution_id": resolution.id,
                "return_reason_id": self.return_reason.id,
            }],
            replacements=replacements,
        )
        return rma

    def _patch_sub_methods(self, rma):
        """Patch the three sub-processors + validate_invoice on the model class."""
        cls = type(rma)
        return (
            patch.object(cls, "validate_invoice", return_value=None),
            patch.object(cls, "process_credit_note", return_value=None),
            patch.object(cls, "process_returned_items", return_value=None),
            patch.object(cls, "process_replacement_items", return_value=None),
        )

    def test_dispatch_refund_only_calls_credit_note(self):
        rma = self._make_rma_with_action(self.resolution_refund)
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_called_once()
            mock_ret.assert_not_called()
            mock_rep.assert_not_called()
            # The refund line should be the one passed
            (passed_lines,) = mock_cn.call_args.args
            self.assertEqual(passed_lines, rma.rma_line_ids)

    def test_dispatch_refund_with_returned_item_calls_credit_note_and_returns(self):
        rma = self._make_rma_with_action(self.resolution_refund_with_return)
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_called_once()
            mock_ret.assert_called_once()
            mock_rep.assert_not_called()
            # Both calls receive the same single line
            (cn_lines,) = mock_cn.call_args.args
            (ret_lines,) = mock_ret.call_args.args
            self.assertEqual(cn_lines, rma.rma_line_ids)
            self.assertEqual(ret_lines, rma.rma_line_ids)

    def test_dispatch_replacement_only_calls_replacement(self):
        rma = self._make_rma_with_action(
            self.resolution_replacement,
            replacements=[{"product_id": self.product_b.id, "qty": 1}],
        )
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_not_called()
            mock_ret.assert_not_called()
            mock_rep.assert_called_once()
            (passed_replacements,) = mock_rep.call_args.args
            self.assertEqual(passed_replacements, rma.replace_prd_ids)

    def test_dispatch_replacement_with_returned_item_calls_credit_returns_and_replacement(self):
        rma = self._make_rma_with_action(
            self.resolution_replacement_with_return,
            replacements=[{"product_id": self.product_b.id, "qty": 1}],
        )
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_called_once()
            mock_ret.assert_called_once()
            mock_rep.assert_called_once()
            (cn_lines,) = mock_cn.call_args.args
            self.assertEqual(cn_lines, rma.rma_line_ids)

    def test_dispatch_replacement_without_replace_prd_ids_skips_replacement_call(self):
        # Edge case: line has replacement action but no replacement products entered
        rma = self._make_rma_with_action(self.resolution_replacement)
        self.assertFalse(rma.replace_prd_ids)
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_not_called()
            mock_ret.assert_not_called()
            mock_rep.assert_not_called()

    def test_dispatch_mixed_actions_routes_all(self):
        rma = self._make_rma(
            rma_type="rma_with_do",
            lines=[
                {
                    "product_id": self.product_a.id,
                    "return_qty": 1,
                    "price_unit": 100.0,
                    "delivery_qty": 5.0,
                    "rma_resolution_id": self.resolution_refund.id,
                    "return_reason_id": self.return_reason.id,
                },
                {
                    "product_id": self.product_b.id,
                    "return_qty": 1,
                    "price_unit": 50.0,
                    "delivery_qty": 5.0,
                    "rma_resolution_id": self.resolution_replacement_with_return.id,
                    "return_reason_id": self.return_reason.id,
                },
            ],
            replacements=[{"product_id": self.product_a.id, "qty": 1}],
        )
        p_vi, p_cn, p_ret, p_rep = self._patch_sub_methods(rma)
        with p_vi, p_cn as mock_cn, p_ret as mock_ret, p_rep as mock_rep:
            rma.process_rma()
            mock_cn.assert_called_once()
            mock_ret.assert_called_once()
            mock_rep.assert_called_once()
            # credit_note gets refund and returned replacement lines; returns
            # gets only the returned replacement line.
            (cn_lines,) = mock_cn.call_args.args
            (ret_lines,) = mock_ret.call_args.args
            self.assertEqual(len(cn_lines), 2)
            self.assertEqual(
                set(cn_lines.mapped("rma_resolution_action")),
                {"refund", "replacement_with_returned_item"},
            )
            self.assertEqual(len(ret_lines), 1)
            self.assertEqual(ret_lines.rma_resolution_action, "replacement_with_returned_item")


@tagged("post_install", "-at_install")
class TestApproveFlow(RmaTestCommon):
    """Verify state transitions through action_approve for each of the 4 flows."""

    def _approve_rma(self, rma):
        """Run action_approve with sub-methods stubbed out."""
        cls = type(rma)
        with patch.object(cls, "validate_invoice", return_value=None), \
             patch.object(cls, "process_credit_note", return_value=None), \
             patch.object(cls, "process_returned_items", return_value=None), \
             patch.object(cls, "process_replacement_items", return_value=None):
            rma.action_approve()

    def _make_approvable(self, resolution, replacements=None):
        return self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 2,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "rma_resolution_id": resolution.id,
                "return_reason_id": self.return_reason.id,
            }],
            replacements=replacements,
            state="submit",
        )

    def test_approve_refund_only_lands_in_approved(self):
        rma = self._make_approvable(self.resolution_refund)
        self._approve_rma(rma)
        self.assertEqual(rma.state, "approved")

    def test_approve_replacement_only_lands_in_approved(self):
        rma = self._make_approvable(
            self.resolution_replacement,
            replacements=[{"product_id": self.product_b.id, "qty": 1}],
        )
        self._approve_rma(rma)
        self.assertEqual(rma.state, "approved")

    def test_process_replacement_items_replaces_existing_draft_order_lines(self):
        rma = self._make_rma(rma_type="rma_with_do")
        first_replacement = self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.product_a.id,
            "qty": 1,
        })
        rma.process_replacement_items(first_replacement)
        replacement_order = self.env["sale.order"].search(
            [("origin", "=", rma.name)], limit=1
        )
        self.assertEqual(len(replacement_order.order_line), 1)
        self.assertEqual(replacement_order.order_line.product_id, self.product_a)

        second_replacement = self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.product_b.id,
            "qty": 2,
        })
        rma.process_replacement_items(second_replacement)

        self.assertEqual(len(replacement_order.order_line), 1)
        self.assertEqual(replacement_order.order_line.product_id, self.product_b)
        self.assertEqual(replacement_order.order_line.product_uom_qty, 2)

    def test_process_replacement_items_uses_pricelist_when_price_not_manual(self):
        pricelist = self.env["product.pricelist"].create({
            "name": "RMA Replacement 20 Percent Off",
            "currency_id": self.env.company.currency_id.id,
        })
        self.env["product.pricelist.item"].create({
            "pricelist_id": pricelist.id,
            "applied_on": "3_global",
            "compute_price": "percentage",
            "percent_price": 20,
        })
        sale_order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": pricelist.id,
        })
        rma = self._make_rma(rma_type="rma_with_do", sale_order=sale_order.id)
        replacement = self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.product_b.id,
            "qty": 2.0,
        })

        rma.process_replacement_items(replacement)

        replacement_order = self.env["sale.order"].search(
            [("origin", "=", rma.name)], limit=1
        )
        self.assertEqual(replacement_order.pricelist_id, pricelist)
        self.assertEqual(replacement_order.order_line.price_unit, 40.0)
        # RMA totals must reflect the same pricelist price the order uses.
        self.assertEqual(replacement.price_unit, 40.0)
        self.assertEqual(replacement.total_price, 80.0)

    def test_process_replacement_items_uses_fractional_qty_and_manual_price(self):
        rma = self._make_rma(rma_type="rma_with_do")
        replacement = self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.product_b.id,
            "qty": 42.5,
            "price_unit_manual": True,
            "manual_price_unit": 1.0,
        })

        rma.process_replacement_items(replacement)

        replacement_order = self.env["sale.order"].search(
            [("origin", "=", rma.name)], limit=1
        )
        self.assertEqual(replacement_order.order_line.product_uom_qty, 42.5)
        self.assertEqual(replacement_order.order_line.price_unit, 1.0)

    def test_process_replacement_items_preserves_manual_zero_price(self):
        rma = self._make_rma(rma_type="rma_with_do")
        replacement = self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.product_b.id,
            "qty": 1.0,
            "price_unit_manual": True,
            "manual_price_unit": 0.0,
        })

        rma.process_replacement_items(replacement)

        replacement_order = self.env["sale.order"].search(
            [("origin", "=", rma.name)], limit=1
        )
        self.assertTrue(replacement.price_unit_manual)
        self.assertEqual(replacement_order.order_line.price_unit, 0.0)

    def test_process_rma_creates_credit_for_replacement_with_returned_item(self):
        sale_line = self.sale_order.order_line
        sale_line.name = "Configure Door - Rockport"
        if "cpq_configuration_description" in sale_line._fields:
            sale_line.cpq_configuration_description = (
                "Rockport, black, double door with configured jamb and hardware"
            )
        replacement_with_return = self.env["rma.resolution"].create({
            "rma_resolution": "Replacement With Returned Item Credit",
            "rma_action": "replacement_with_returned_item",
        })
        rma = self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 2,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "sale_line_id": sale_line.id,
                "rma_resolution_id": replacement_with_return.id,
                "return_reason_id": self.return_reason.id,
            }],
            replacements=[{
                "product_id": self.product_b.id,
                "qty": 1,
                "price_unit_manual": True,
                "manual_price_unit": 25.0,
            }],
        )

        cls = type(rma)
        with patch.object(cls, "validate_invoice", return_value=None), \
             patch.object(cls, "process_returned_items", return_value=None), \
             patch.object(cls, "process_replacement_items", return_value=None):
            rma.process_rma()

        credit_note = self.env["account.move"].search([
            ("rma_id", "=", rma.id),
            ("move_type", "=", "out_refund"),
        ])
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.state, "draft")
        self.assertEqual(credit_note.partner_id, rma.partner_id)
        self.assertEqual(credit_note.invoice_line_ids.product_id, self.product_a)
        self.assertEqual(
            credit_note.invoice_line_ids.name,
            rma.rma_line_ids.original_description,
        )
        self.assertEqual(credit_note.invoice_line_ids.quantity, 2)
        self.assertEqual(credit_note.invoice_line_ids.price_unit, 100.0)


@tagged("post_install", "-at_install")
class TestCloseFlow(RmaTestCommon):
    """Close should be reachable from both 'approved' (no return picking) and 'processing'."""

    def test_close_from_approved_succeeds_when_no_pickings(self):
        """Refund-only and replacement-only flows have no return picking;
        action_close must work directly from 'approved'."""
        rma = self._make_rma()
        rma.write({"state": "approved"})
        rma.action_close()
        self.assertEqual(rma.state, "close")

    def test_close_from_processing_succeeds(self):
        rma = self._make_rma()
        rma.write({"state": "processing"})
        rma.action_close()
        self.assertEqual(rma.state, "close")

    def test_close_raises_when_picking_pending(self):
        rma = self._make_rma()
        rma.write({"state": "processing"})
        # Create a pending picking linked to this RMA
        picking_type = self.env["stock.picking.type"].search(
            [("code", "=", "incoming")], limit=1
        )
        self.env["stock.picking"].create({
            "picking_type_id": picking_type.id,
            "location_id": picking_type.default_location_src_id.id
                or self.env.ref("stock.stock_location_suppliers").id,
            "location_dest_id": picking_type.default_location_dest_id.id
                or self.env.ref("stock.stock_location_stock").id,
            "rma_id": rma.id,
        })
        with self.assertRaises(ValidationError):
            rma.action_close()
