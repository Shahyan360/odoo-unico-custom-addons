import re

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoDeliverySlipDescriptions(TransactionCase):
    """Delivery slip must carry the sale line description, and must list
    configured products one row per unit."""

    @staticmethod
    def _html_text(html_bytes):
        text = html_bytes.decode()
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.partner = cls.env["res.partner"].create({
            "name": "Delivery Slip Customer",
        })
        cls.plain_product = cls.env["product.product"].create({
            "name": "Plain Slab Door",
            "is_storable": True,
        })
        cls.configured_product = cls.env["product.product"].create({
            "name": "Configure Door",
            "is_storable": True,
            "is_klouded_configurable": True,
        })
        cls.order = cls.env["sale.order"].create({"partner_id": cls.partner.id})

    def _create_sale_line(self, product, qty, name):
        vals = {
            "order_id": self.order.id,
            "product_id": product.id,
            "product_uom_qty": qty,
            "name": name,
        }
        fields_ = self.env["sale.order.line"]._fields
        # This module refuses to save a short-stock stockable line without a
        # purchase note; these fixtures hold no stock.
        if "unico_purchase_note" in fields_:
            vals["unico_purchase_note"] = "Delivery slip test sourcing note"
        # CPQ owns the description of a configured line and rewrites `name`
        # to "<product> - <door style>" on save, which would wipe the text
        # under test. A salesperson editing the description turns this off the
        # same way, so do it explicitly rather than depend on the heuristic.
        if "cpq_display_name_managed" in fields_ and product.is_klouded_configurable:
            vals["cpq_display_name_managed"] = False
        return self.env["sale.order.line"].create(vals)

    def _create_picking(self, sale_line, qty):
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.warehouse.out_type_id.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "origin": self.order.name,
            "sale_id": self.order.id,
        })
        self.env["stock.move"].create({
            "product_id": sale_line.product_id.id,
            "product_uom_qty": qty,
            "product_uom": sale_line.product_id.uom_id.id,
            "picking_id": picking.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "sale_line_id": sale_line.id,
        })
        return picking

    def _validate(self, picking):
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.button_validate()
        return picking

    def _render(self, picking):
        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "stock.action_report_delivery",
            picking.ids,
        )
        return self._html_text(html)

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------
    def test_description_helper_merges_sources_without_repeats(self):
        sale_line = self._create_sale_line(
            self.plain_product, 1.0, "Plain Slab Door\nPrimed, bore for privacy set",
        )
        picking = self._create_picking(sale_line, 1.0)
        move = picking.move_ids

        description = move._unico_delivery_slip_description()

        self.assertIn("Primed, bore for privacy set", description)
        # The product name itself is already printed above the description.
        self.assertNotIn(self.plain_product.name, description)
        # description_picking also starts with the product name -> printed once.
        self.assertEqual(
            description.count("Primed, bore for privacy set"), 1,
        )

    def test_delivery_slip_prints_sale_line_description_when_done(self):
        sale_line = self._create_sale_line(
            self.plain_product, 1.0, "Plain Slab Door\nDeliver to rear gate",
        )
        picking = self._validate(self._create_picking(sale_line, 1.0))

        rendered = self._render(picking)

        self.assertEqual(picking.state, "done")
        self.assertIn("Deliver to rear gate", rendered)

    def test_delivery_slip_prints_sale_line_description_before_done(self):
        sale_line = self._create_sale_line(
            self.plain_product, 1.0, "Plain Slab Door\nDeliver to rear gate",
        )
        picking = self._create_picking(sale_line, 1.0)
        picking.action_confirm()

        rendered = self._render(picking)

        self.assertNotEqual(picking.state, "done")
        self.assertIn("Deliver to rear gate", rendered)

    def test_same_product_different_descriptions_stay_on_separate_rows(self):
        first = self._create_sale_line(
            self.plain_product, 1.0, "Plain Slab Door\nUnit A finish",
        )
        second = self._create_sale_line(
            self.plain_product, 1.0, "Plain Slab Door\nUnit B finish",
        )
        picking = self._create_picking(first, 1.0)
        self.env["stock.move"].create({
            "product_id": self.plain_product.id,
            "product_uom_qty": 1.0,
            "product_uom": self.plain_product.uom_id.id,
            "picking_id": picking.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "sale_line_id": second.id,
        })
        picking = self._validate(picking)

        rendered = self._render(picking)

        self.assertIn("Unit A finish", rendered)
        self.assertIn("Unit B finish", rendered)

    # ------------------------------------------------------------------
    # One row per configured unit
    # ------------------------------------------------------------------
    def test_configured_line_splits_into_one_row_per_unit_when_done(self):
        sale_line = self._create_sale_line(
            self.configured_product, 3.0, "Configure Door - Shaker\n30x80 LH prehung",
        )
        picking = self._validate(self._create_picking(sale_line, 3.0))

        aggregated = picking.move_ids.move_line_ids._get_aggregated_product_quantities()

        self.assertEqual(len(aggregated), 3)
        for values in aggregated.values():
            self.assertEqual(values["quantity"], 1.0)
            self.assertEqual(values["qty_ordered"], 1.0)
            self.assertIn("30x80 LH prehung", values["description"])

    def test_configured_line_splits_into_one_row_per_unit_before_done(self):
        sale_line = self._create_sale_line(
            self.configured_product, 3.0, "Configure Door - Shaker\n30x80 LH prehung",
        )
        picking = self._create_picking(sale_line, 3.0)
        picking.action_confirm()

        rows = picking.move_ids._unico_delivery_slip_move_rows()

        self.assertEqual(len(rows), 3)
        self.assertEqual([row["product_uom_qty"] for row in rows], [1.0, 1.0, 1.0])
        rendered = self._render(picking)
        self.assertEqual(rendered.count("30x80 LH prehung"), 3)

    def test_plain_product_keeps_a_single_aggregated_row(self):
        sale_line = self._create_sale_line(self.plain_product, 3.0, "Plain Slab Door")
        picking = self._validate(self._create_picking(sale_line, 3.0))

        aggregated = picking.move_ids.move_line_ids._get_aggregated_product_quantities()

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(list(aggregated.values())[0]["quantity"], 3.0)

    def test_short_shipped_configured_line_shows_the_undelivered_unit(self):
        rows = self.env["stock.move"]._unico_split_unit_quantities(3.0, 2.0, 3)

        self.assertEqual(rows, [(1.0, 1.0), (1.0, 1.0), (1.0, 0.0)])

    def test_fractional_quantity_is_not_split(self):
        self.assertEqual(
            self.env["stock.move"]._unico_unit_split_count(2.5, 2.5), 1,
        )
        self.assertEqual(
            self.env["stock.move"]._unico_unit_split_count(3.0, 2.0), 3,
        )
