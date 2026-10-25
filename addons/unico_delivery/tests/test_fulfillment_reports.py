import re

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoFulfillmentReports(TransactionCase):

    @staticmethod
    def _html_text(html_bytes):
        text = html_bytes.decode()
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "Fulfillment Report Customer",
            "street": "100 Billing Road",
            "city": "Houston",
            "state_id": cls.env.ref("base.state_us_43").id,
            "zip": "77001",
            "country_id": cls.env.ref("base.us").id,
        })
        cls.shipping_partner = cls.env["res.partner"].create({
            "name": "Fulfillment Report Job Site",
            "parent_id": cls.partner.id,
            "type": "delivery",
            "street": "221 Jobsite Way",
            "city": "Austin",
            "state_id": cls.env.ref("base.state_us_43").id,
            "zip": "78701",
            "country_id": cls.env.ref("base.us").id,
        })
        cls.product = cls.env["product.product"].create({
            "name": "Fulfillment Report Door",
            "list_price": 100.0,
        })
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "partner_shipping_id": cls.shipping_partner.id,
            "fulfillment_type": "will_call",
        })
        cls.sale_line = cls.env["sale.order.line"].create({
            "order_id": cls.order.id,
            "product_id": cls.product.id,
            "product_uom_qty": 1.0,
            "price_unit": 100.0,
        })

    def _create_picking(self):
        warehouse = self.env.ref("stock.warehouse0")
        customer_location = self.env.ref("stock.stock_location_customers")
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": warehouse.out_type_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": customer_location.id,
            "origin": self.order.name,
            "sale_id": self.order.id,
        })
        self.env["stock.move"].create({
            "product_id": self.product.id,
            "product_uom_qty": 1.0,
            "product_uom": self.product.uom_id.id,
            "picking_id": picking.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": customer_location.id,
            "sale_line_id": self.sale_line.id,
        })
        return picking

    def _create_production(self):
        return self.env["mrp.production"].create({
            "product_id": self.product.id,
            "product_qty": 1.0,
            "product_uom_id": self.product.uom_id.id,
            "origin": self.order.name,
            "sale_line_id": self.sale_line.id,
        })

    def test_picking_operation_report_prints_fulfillment_type(self):
        picking = self._create_picking()

        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "stock.action_report_picking",
            picking.ids,
        )
        rendered = self._html_text(html)

        self.assertEqual(picking.fulfillment_type, "will_call")
        self.assertIn("Fulfillment Type:", rendered)
        self.assertIn("Will Call/Customer Pickup", rendered)

    def test_delivery_slip_prints_fulfillment_type(self):
        picking = self._create_picking()

        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "stock.action_report_delivery",
            picking.ids,
        )
        rendered = self._html_text(html)

        self.assertIn("Fulfillment Type", rendered)
        self.assertIn("Will Call/Customer Pickup", rendered)

    def test_picking_real_address_uses_sale_order_shipping_address(self):
        picking = self._create_picking()

        self.assertIn("221 Jobsite Way", picking.unico_delivery_address_display)
        self.assertIn("Austin", picking.unico_delivery_address_display)
        self.assertNotIn("100 Billing Road", picking.unico_delivery_address_display)

    def test_legacy_regenerate_pickings_action_opens_delivery_orders(self):
        picking = self._create_picking()

        action = self.order.action_unico_regenerate_pickings()

        self.assertEqual(action["res_model"], "stock.picking")
        self.assertEqual(action["domain"], [("id", "in", picking.ids)])

    def test_production_ticket_prints_fulfillment_type(self):
        production = self._create_production()

        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "mrp.action_report_production_order",
            production.ids,
        )
        rendered = self._html_text(html)

        self.assertEqual(production.fulfillment_type, "will_call")
        self.assertIn("Fulfillment Type:", rendered)
        self.assertIn("Will Call/Customer Pickup", rendered)
