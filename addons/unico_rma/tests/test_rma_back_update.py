"""Tests for syncing the verified return qty back onto the credit note.

When the warehouse validates a return picking with fewer units than the
RMA originally requested (e.g. 50 of 100), the draft credit note line
quantity must drop to 50 and any percentage-based restocking fee must
recompute against the new subtotal. Without this, the customer gets
refunded too much and the fee is wrong.
"""
from unittest.mock import patch

from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestSyncReturnToCreditNote(RmaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_refund_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund With Return",
            "rma_action": "refund_with_returned_item",
        })
        cls.resolution_refund_with_fee = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund With Restocking Fee",
            "rma_action": "refund_with_returned_item",
            "apply_return_fee": True,
            "return_fee_type": "percentage",
            "return_fee_value": 10.0,
        })

        cls.picking_type_in = cls.env["stock.picking.type"].search(
            [("code", "=", "incoming")], limit=1
        )
        cls.picking_type_out = cls.env["stock.picking.type"].search(
            [("code", "=", "outgoing")], limit=1
        )
        cls.loc_supplier = cls.env.ref("stock.stock_location_suppliers")
        cls.loc_stock = cls.env.ref("stock.stock_location_stock")
        cls.fee_product = cls.env.ref("unico_rma.product_return_fee")

    def _make_refund_rma(self, resolution, return_qty=100, price_unit=100.0):
        return self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": return_qty,
                "price_unit": price_unit,
                "delivery_qty": return_qty,
                "rma_resolution_id": resolution.id,
                "return_reason_id": self.return_reason.id,
            }],
            state="approved",
        )

    def _make_credit_note(self, rma, *, fee_amount=None):
        """Build a draft credit note mirroring what process_credit_note creates."""
        invoice_line_vals = []
        for rma_line in rma.rma_line_ids:
            invoice_line_vals.append((0, 0, {
                "product_id": rma_line.product_id.id,
                "quantity": rma_line.return_qty,
                "price_unit": rma_line.price_unit,
                "rma_line_id": rma_line.id,
            }))
        if fee_amount is not None:
            invoice_line_vals.append((0, 0, {
                "product_id": self.fee_product.id,
                "name": "Return Fee",
                "quantity": -1.0,
                "price_unit": fee_amount,
            }))
        return self.env["account.move"].create({
            "move_type": "out_refund",
            "partner_id": self.partner.id,
            "rma_id": rma.id,
            "invoice_line_ids": invoice_line_vals,
        })

    def _make_return_picking(self, rma, rma_line, received_qty, picking_type=None):
        picking_type = picking_type or self.picking_type_in
        src, dest = self.loc_supplier, self.loc_stock
        if picking_type.code == "outgoing":
            src, dest = self.loc_stock, self.loc_supplier
        picking = self.env["stock.picking"].create({
            "picking_type_id": picking_type.id,
            "location_id": src.id,
            "location_dest_id": dest.id,
            "rma_id": rma.id,
        })
        move = self.env["stock.move"].create({
            "product_id": rma_line.product_id.id,
            "product_uom": rma_line.product_id.uom_id.id,
            "product_uom_qty": rma_line.return_qty,
            "quantity": received_qty,
            "picking_id": picking.id,
            "location_id": src.id,
            "location_dest_id": dest.id,
            "rma_line_id": rma_line.id,
        })
        move.state = "done"
        picking.state = "done"
        return picking

    def _cn_line_for(self, credit_note, rma_line):
        return credit_note.invoice_line_ids.filtered(
            lambda l: l.rma_line_id == rma_line
        )

    def test_sync_writes_received_qty_to_rma_line_and_credit_note(self):
        rma = self._make_refund_rma(self.resolution_refund_with_return, return_qty=100)
        rma_line = rma.rma_line_ids
        cn = self._make_credit_note(rma)
        cn_line = self._cn_line_for(cn, rma_line)
        self.assertEqual(cn_line.quantity, 100)

        picking = self._make_return_picking(rma, rma_line, received_qty=50)
        rma._sync_return_to_credit_note(picking)

        self.assertEqual(rma_line.received_qty, 50)
        self.assertEqual(rma_line.return_qty, 50)
        self.assertEqual(cn_line.quantity, 50)

    def test_sync_is_noop_when_received_equals_requested(self):
        rma = self._make_refund_rma(self.resolution_refund_with_return, return_qty=10)
        rma_line = rma.rma_line_ids
        cn = self._make_credit_note(rma)
        cn_line = self._cn_line_for(cn, rma_line)

        picking = self._make_return_picking(rma, rma_line, received_qty=10)
        rma._sync_return_to_credit_note(picking)

        self.assertEqual(rma_line.return_qty, 10)
        self.assertEqual(cn_line.quantity, 10)

    def test_sync_recomputes_percentage_restocking_fee(self):
        rma = self._make_refund_rma(self.resolution_refund_with_fee, return_qty=100)
        rma_line = rma.rma_line_ids
        # Initial fee: 100 units * 100.0 * 10% = 1000
        cn = self._make_credit_note(rma, fee_amount=1000.0)
        fee_lines = cn.invoice_line_ids.filtered(
            lambda l: l.product_id == self.fee_product
        )
        self.assertEqual(len(fee_lines), 1)
        self.assertEqual(fee_lines.price_unit, 1000.0)

        picking = self._make_return_picking(rma, rma_line, received_qty=50)
        rma._sync_return_to_credit_note(picking)

        fee_lines = cn.invoice_line_ids.filtered(
            lambda l: l.product_id == self.fee_product
        )
        # Exactly one fee line remains (no duplicates) and it's recomputed:
        # 50 * 100.0 * 10% = 500
        self.assertEqual(len(fee_lines), 1)
        self.assertEqual(fee_lines.price_unit, 500.0)

    def test_sync_aggregates_quantities_across_multiple_pickings(self):
        rma = self._make_refund_rma(self.resolution_refund_with_return, return_qty=100)
        rma_line = rma.rma_line_ids
        cn = self._make_credit_note(rma)
        cn_line = self._cn_line_for(cn, rma_line)

        self._make_return_picking(rma, rma_line, received_qty=30)
        second = self._make_return_picking(rma, rma_line, received_qty=20)
        rma._sync_return_to_credit_note(second)

        self.assertEqual(rma_line.return_qty, 50)
        self.assertEqual(cn_line.quantity, 50)

    def test_sync_skips_when_picking_is_outgoing(self):
        rma = self._make_refund_rma(self.resolution_refund_with_return, return_qty=100)
        rma_line = rma.rma_line_ids
        cn = self._make_credit_note(rma)
        cn_line = self._cn_line_for(cn, rma_line)

        out_picking = self._make_return_picking(
            rma, rma_line, received_qty=50, picking_type=self.picking_type_out
        )
        rma._sync_return_to_credit_note(out_picking)

        self.assertEqual(rma_line.return_qty, 100)
        self.assertEqual(cn_line.quantity, 100)

    def test_sync_does_not_touch_posted_credit_notes(self):
        rma = self._make_refund_rma(self.resolution_refund_with_return, return_qty=100)
        rma_line = rma.rma_line_ids
        cn = self._make_credit_note(rma)
        cn_line = self._cn_line_for(cn, rma_line)
        # Simulate the credit note already being posted by hiding it from the
        # draft-only search inside _sync_return_to_credit_note.
        AccountMove = type(cn)
        original_search = AccountMove.search

        def search_skip_posted(records, domain, *args, **kwargs):
            if ("state", "=", "draft") in domain:
                return records.browse([])
            return original_search(records, domain, *args, **kwargs)

        picking = self._make_return_picking(rma, rma_line, received_qty=50)
        with patch.object(AccountMove, "search", search_skip_posted):
            rma._sync_return_to_credit_note(picking)

        # rma.lines still gets the verified qty written back...
        self.assertEqual(rma_line.return_qty, 50)
        # ...but the (faked-as-posted) credit note line is untouched.
        self.assertEqual(cn_line.quantity, 100)

    def test_sync_fee_helper_is_idempotent(self):
        rma = self._make_refund_rma(self.resolution_refund_with_fee, return_qty=40)
        cn = self._make_credit_note(rma, fee_amount=400.0)

        rma._sync_credit_note_fee_lines(cn)
        rma._sync_credit_note_fee_lines(cn)

        fee_lines = cn.invoice_line_ids.filtered(
            lambda l: l.product_id == self.fee_product
        )
        # Calling twice should still leave exactly one fee line.
        self.assertEqual(len(fee_lines), 1)
        # 40 * 100.0 * 10% = 400
        self.assertEqual(fee_lines.price_unit, 400.0)
