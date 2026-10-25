"""Mapping RMA lines onto the delivery's return-wizard lines.

The return wizard builds one line per delivery move, every quantity starting
at zero, and only the ones we fill end up on the return transfer. Matching
purely on ``rma_line.product_id`` broke configured products — a Configure Door
ships as its manufacturing order's finished good, which need not be the
product recorded on the RMA line — so nothing matched, every quantity stayed
zero and Approve died on stock's generic "Please specify at least one non-zero
quantity" with no indication of which line was at fault.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestReturnWizardMatching(RmaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_refund_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund With Return",
            "rma_action": "refund_with_returned_item",
        })
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.sale_line = cls.sale_order.order_line[:1]

    def _deliver(self, product, qty=1.0, sale_line=None):
        """A done outgoing picking, as the RMA's delivery_order."""
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.warehouse.out_type_id.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "origin": self.sale_order.name,
        })
        self.env["stock.move"].create({
            "product_id": product.id,
            "product_uom_qty": qty,
            "product_uom": product.uom_id.id,
            "picking_id": picking.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "sale_line_id": (sale_line or self.sale_line).id,
        })
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.button_validate()
        return picking

    def _make_return_rma(self, picking, product, return_qty=1.0):
        return self._make_rma(
            rma_type="rma_with_do",
            delivery_order=picking.id,
            lines=[{
                "product_id": product.id,
                "sale_line_id": self.sale_line.id,
                "return_qty": return_qty,
                "delivery_qty": return_qty,
                "price_unit": 100.0,
                "rma_resolution_id": self.resolution_refund_with_return.id,
                "return_reason_id": self.return_reason.id,
            }],
        )

    def _returned_qty(self, rma):
        returns = rma._get_all_rma_pickings().filtered(
            lambda p: p.picking_type_id.code == "incoming"
        )
        return sum(returns.move_ids.mapped("product_uom_qty"))

    # ------------------------------------------------------------------
    def test_matches_when_delivered_product_is_the_rma_product(self):
        """The ordinary case has to keep working."""
        picking = self._deliver(self.product_a, qty=3.0)
        rma = self._make_return_rma(picking, self.product_a, return_qty=2.0)

        rma.process_returned_items(rma.rma_line_ids)

        self.assertEqual(self._returned_qty(rma), 2.0)

    def test_matches_when_delivered_product_differs_from_rma_product(self):
        """The Configure Door case: the delivery shipped a different product
        against the same sale line, and it is the only move on it."""
        picking = self._deliver(self.product_b, qty=1.0)
        rma = self._make_return_rma(picking, self.product_a, return_qty=1.0)

        rma.process_returned_items(rma.rma_line_ids)

        returns = rma._get_all_rma_pickings().filtered(
            lambda p: p.picking_type_id.code == "incoming"
        )
        self.assertEqual(returns.move_ids.product_id, self.product_b)
        self.assertEqual(self._returned_qty(rma), 1.0)

    def test_return_qty_is_spread_over_several_delivery_moves(self):
        picking = self._deliver(self.product_a, qty=2.0)
        self.env["stock.move"].create({
            "product_id": self.product_a.id,
            "product_uom_qty": 2.0,
            "product_uom": self.product_a.uom_id.id,
            "picking_id": picking.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "sale_line_id": self.sale_line.id,
        })
        rma = self._make_return_rma(picking, self.product_a, return_qty=3.0)

        rma.process_returned_items(rma.rma_line_ids)

        self.assertEqual(self._returned_qty(rma), 3.0)

    def test_unmatched_line_reports_itself_instead_of_the_core_message(self):
        """With no sale order line there is nothing to trace the line by, so
        it falls back to the RMA's own delivery — and that delivery never
        shipped this product."""
        picking = self._deliver(self.product_a, qty=1.0)
        rma = self._make_return_rma(picking, self.product_b)
        rma.rma_line_ids.sale_line_id = False

        with self.assertRaises(UserError) as caught:
            rma.process_returned_items(rma.rma_line_ids)

        message = str(caught.exception)
        self.assertIn(self.product_b.display_name, message)
        self.assertIn(picking.name, message)
        self.assertNotIn("non-zero quantity", message)

    def test_return_moves_are_linked_back_to_their_rma_line(self):
        """_sync_return_to_credit_note needs this link to update the credit
        note when the warehouse receives less than was claimed."""
        picking = self._deliver(self.product_b, qty=1.0)
        rma = self._make_return_rma(picking, self.product_a, return_qty=1.0)

        rma.process_returned_items(rma.rma_line_ids)

        returns = rma._get_all_rma_pickings().filtered(
            lambda p: p.picking_type_id.code == "incoming"
        )
        self.assertEqual(returns.move_ids.rma_line_id, rma.rma_line_ids)

    # ------------------------------------------------------------------
    # Orders that ship as several deliveries
    # ------------------------------------------------------------------
    def test_line_returns_to_the_delivery_that_actually_shipped_it(self):
        """RMA-000080: a configured door leaves from Post-Production on its
        own transfer while stocked goods leave from Output, and the RMA header
        names whichever came first. The line must still go back to the
        delivery that shipped it."""
        other_line = self.env["sale.order.line"].create({
            "order_id": self.sale_order.id,
            "product_id": self.product_b.id,
            "product_uom_qty": 1.0,
            "price_unit": 50.0,
        })
        header_picking = self._deliver(self.product_b, qty=1.0, sale_line=other_line)
        real_picking = self._deliver(self.product_a, qty=1.0)
        # The header points at the delivery that did NOT ship the RMA'd line.
        rma = self._make_return_rma(header_picking, self.product_a, return_qty=1.0)

        rma.process_returned_items(rma.rma_line_ids)

        returns = rma._get_all_rma_pickings().filtered(
            lambda p: p.picking_type_id.code == "incoming"
        )
        self.assertEqual(returns.move_ids.product_id, self.product_a)
        self.assertEqual(
            returns.move_ids.origin_returned_move_id.picking_id, real_picking,
        )

    def test_lines_from_two_deliveries_produce_two_returns(self):
        second_line = self.env["sale.order.line"].create({
            "order_id": self.sale_order.id,
            "product_id": self.product_b.id,
            "product_uom_qty": 1.0,
            "price_unit": 50.0,
        })
        first_picking = self._deliver(self.product_a, qty=1.0)
        second_picking = self._deliver(self.product_b, qty=1.0, sale_line=second_line)
        rma = self._make_return_rma(first_picking, self.product_a, return_qty=1.0)
        rma.write({"rma_line_ids": [(0, 0, {
            "product_id": self.product_b.id,
            "sale_line_id": second_line.id,
            "return_qty": 1.0,
            "delivery_qty": 1.0,
            "price_unit": 50.0,
            "rma_resolution_id": self.resolution_refund_with_return.id,
            "return_reason_id": self.return_reason.id,
        })]})

        rma.process_returned_items(rma.rma_line_ids)

        returns = rma._get_all_rma_pickings().filtered(
            lambda p: p.picking_type_id.code == "incoming"
        )
        self.assertEqual(len(returns), 2)
        self.assertEqual(
            returns.move_ids.origin_returned_move_id.picking_id,
            first_picking | second_picking,
        )
        # Each return move still resolves back to its own RMA line.
        self.assertEqual(returns.move_ids.rma_line_id, rma.rma_line_ids)

    def test_never_shipped_line_names_the_order_not_the_delivery(self):
        never_shipped = self.env["sale.order.line"].create({
            "order_id": self.sale_order.id,
            "product_id": self.product_b.id,
            "product_uom_qty": 1.0,
            "price_unit": 50.0,
        })
        picking = self._deliver(self.product_a, qty=1.0)
        rma = self._make_return_rma(picking, self.product_b)
        rma.rma_line_ids.sale_line_id = never_shipped

        with self.assertRaises(UserError) as caught:
            rma.process_returned_items(rma.rma_line_ids)

        message = str(caught.exception)
        self.assertIn("never shipped", message)
        self.assertIn(self.product_b.display_name, message)
