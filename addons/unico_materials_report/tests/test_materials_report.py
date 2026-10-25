# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMaterialsReport(TransactionCase):
    """Covers the deterministic 'awaiting stock' path plus the order rollup,
    urgency bucketing, cron and dashboard payload. The 'awaiting manufacturing'
    (MO/SFP) and 'awaiting purchase' paths share the same compute and are
    exercised end-to-end in the manual verification steps (they need a CPQ BOM /
    special-component fixture)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaleOrder = cls.env["sale.order"]
        cls.customer = cls.env["res.partner"].create({"name": "Materials Test Customer"})
        # Storable product with zero stock -> a confirmed line is short on stock.
        storable_tmpl = cls.env["product.template"].create({
            "name": "Plain Storable Door Slab",
            "type": "consu",
            "is_storable": True,
            "list_price": 100.0,
        })
        cls.storable = storable_tmpl.product_variant_id
        cls.service = cls.env["product.product"].create({
            "name": "Installation Service",
            "type": "service",
            "list_price": 50.0,
        })

    def _confirm_order(self, lines):
        order = self.SaleOrder.create({
            "partner_id": self.customer.id,
            "order_line": lines,
        })
        order.action_confirm()
        return order

    def test_stock_waiting(self):
        order = self._confirm_order([(0, 0, {
            "product_id": self.storable.id,
            "product_uom_qty": 5,
            "unico_purchase_note": "Source from vendor",  # required for short stock
        })])
        line = order.order_line
        self.assertEqual(line.unico_material_wait_reasons, "stock")
        self.assertTrue(line.unico_material_waiting)
        self.assertEqual(line.unico_material_wait_label, "Awaiting Stock")
        self.assertTrue(order.unico_waiting_on_materials)
        self.assertIn("stock", order.unico_material_blocking_reasons)
        self.assertEqual(order.unico_waiting_line_count, 1)
        self.assertIn("Awaiting Stock", order.unico_material_blocking_label)

    def test_service_line_not_waiting(self):
        order = self._confirm_order([(0, 0, {
            "product_id": self.service.id,
            "product_uom_qty": 1,
        })])
        self.assertFalse(order.order_line.unico_material_wait_reasons)
        self.assertFalse(order.unico_waiting_on_materials)
        self.assertEqual(order.unico_material_urgency, "none")

    def test_quotation_not_waiting(self):
        # Same short-stock line but left as a draft quotation -> out of scope.
        order = self.SaleOrder.create({
            "partner_id": self.customer.id,
            "order_line": [(0, 0, {
                "product_id": self.storable.id,
                "product_uom_qty": 5,
                "unico_purchase_note": "Source from vendor",
            })],
        })
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.unico_waiting_on_materials)

    def test_urgency_buckets(self):
        order = self._confirm_order([(0, 0, {
            "product_id": self.storable.id,
            "product_uom_qty": 5,
            "unico_purchase_note": "Source from vendor",
        })])
        now = fields.Datetime.now()
        order.commitment_date = now - timedelta(days=1)
        self.assertEqual(order.unico_material_urgency, "overdue")
        order.commitment_date = now + timedelta(days=2)
        self.assertEqual(order.unico_material_urgency, "urgent")
        order.commitment_date = now + timedelta(days=10)
        self.assertEqual(order.unico_material_urgency, "scheduled")

    def test_cron_refresh_is_safe(self):
        order = self._confirm_order([(0, 0, {
            "product_id": self.storable.id,
            "product_uom_qty": 5,
            "unico_purchase_note": "Source from vendor",
        })])
        self.SaleOrder._cron_recompute_unico_material_status()
        self.assertTrue(order.unico_waiting_on_materials)

    def test_dashboard_payload(self):
        self._confirm_order([(0, 0, {
            "product_id": self.storable.id,
            "product_uom_qty": 5,
            "unico_purchase_note": "Source from vendor",
        })])
        data = self.SaleOrder.retrieve_unico_materials_dashboard()
        self.assertGreaterEqual(data["total"], 1)
        self.assertEqual({tile["key"] for tile in data["urgency"]},
                         {"overdue", "urgent", "scheduled"})
        self.assertEqual({tile["key"] for tile in data["reasons"]},
                         {"stock", "purchase", "manufacturing"})
        for tile in data["urgency"] + data["reasons"]:
            self.assertTrue(tile["domain"])
