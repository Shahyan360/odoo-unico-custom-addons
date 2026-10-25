from unittest.mock import patch

from odoo import fields
from odoo.exceptions import LockError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

# These tests deliberately drive the special-RFQ error/cleanup paths, which log
# expected WARNING/ERROR lines. Mute that logger on those tests so the build log
# stays clean and a real warning is not lost in the noise.
_RFQ_LOGGER = "odoo.addons.unico_delivery.models.sale_order_line"


@tagged("post_install", "-at_install")
class TestUnicoSpecialRfqAutomation(TransactionCase):
    """Production guards for Unico's non-blocking special RFQ automation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({"name": "RFQ Automation Customer"})
        cls.vendor_a = cls.env["res.partner"].create({
            "name": "RFQ Automation Vendor A",
            "supplier_rank": 1,
        })
        cls.vendor_b = cls.env["res.partner"].create({
            "name": "RFQ Automation Vendor B",
            "supplier_rank": 1,
        })
        cls.sale_product = cls.env["product.template"].create({
            "name": "Configured Special Door",
            "type": "consu",
            "list_price": 100.0,
            "is_klouded_special_order": True,
        }).product_variant_id
        cls.component_product = cls.env["product.template"].create({
            "name": "Vendor Special Door Component",
            "type": "consu",
            "purchase_ok": True,
            "standard_price": 25.0,
        }).product_variant_id
        cls.component_product_b = cls.env["product.template"].create({
            "name": "Vendor Special Hardware Component",
            "type": "consu",
            "purchase_ok": True,
            "standard_price": 15.0,
        }).product_variant_id

    def _create_ready_special_line(self, vendor=None, quantity=2.0, cost=25.0):
        vendor = vendor or self.vendor_a
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
        })
        line = self.env["sale.order.line"].create({
            "order_id": order.id,
            "product_id": self.sale_product.id,
            "product_uom_qty": 1.0,
            "price_unit": 100.0,
            "cpq_special_vendor_id": vendor.id,
        })
        component = self.env["sale.order.line.cpq.special.component"].create({
            "sale_line_id": line.id,
            "component_type": "inventory_item",
            "product_id": self.component_product.id,
            "name": "Vendor Special Door Component",
            "vendor_id": vendor.id,
            "product_uom_id": self.component_product.uom_id.id,
            "quantity": quantity,
            "quoted_unit_cost": cost,
            "pricing_cost_source": "quote",
            "sourcing_method": "purchase",
        })
        return order, line, component

    def _create_custom_door_with_rfq(self, cost=40.0, sale_qty=1.0):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
        })
        line = self.env["sale.order.line"].create({
            "order_id": order.id,
            "product_id": self.sale_product.id,
            "product_uom_qty": sale_qty,
            "price_unit": 100.0,
        })
        wizard = self.env["cpq.special.order.wizard"].create({
            "order_line_id": line.id,
            "product_template_id": self.sale_product.product_tmpl_id.id,
            "workflow_mode": "custom_door",
            "door_description": "Customer Lobby Door",
            "door_product_name": "Vendor Lobby Door",
            "door_vendor_sku": "VENDOR-DOOR-100",
            "vendor_id": self.vendor_a.id,
            "configuration_type": "slab_only",
            "door_unit_cost": cost,
            "fulfillment_mode": "purchase",
        })
        wizard.action_confirm()
        line.invalidate_recordset()
        component = line.cpq_special_component_ids
        return order, line, component, component.purchase_order_id

    def test_auto_special_rfq_creates_and_links_purchase_document(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)

        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component.invalidate_recordset()

        purchase = line.cpq_special_purchase_ids
        self.assertEqual(len(purchase), 1)
        self.assertTrue(purchase.unico_auto_special_rfq)
        self.assertEqual(purchase.partner_id, self.vendor_a)
        self.assertEqual(purchase.order_line.product_id, self.component_product)
        self.assertAlmostEqual(purchase.order_line.product_qty, 2.0, places=2)
        self.assertAlmostEqual(purchase.order_line.price_unit, 25.0, places=2)
        self.assertEqual(component.purchase_order_id, purchase)
        self.assertEqual(component.purchase_line_id, purchase.order_line)

    def test_custom_door_sale_qty_scales_and_refreshes_draft_rfq(self):
        _order, line, component, purchase = self._create_custom_door_with_rfq(
            sale_qty=2.0,
        )
        purchase_line = component.purchase_line_id
        lock_order = []
        line_lock = type(line).lock_for_update
        purchase_lock = type(purchase).lock_for_update

        def track_line_lock(records, **kwargs):
            lock_order.append("sale_line")
            return line_lock(records, **kwargs)

        def track_purchase_lock(records, **kwargs):
            lock_order.append("purchase")
            return purchase_lock(records, **kwargs)

        self.assertAlmostEqual(component.quantity, 1.0, places=2)
        self.assertAlmostEqual(purchase_line.product_qty, 2.0, places=2)

        with patch.object(
            type(line),
            "lock_for_update",
            autospec=True,
            side_effect=track_line_lock,
        ), patch.object(
            type(purchase),
            "lock_for_update",
            autospec=True,
            side_effect=track_purchase_lock,
        ):
            line.write({"product_uom_qty": 4.0})
        purchase.invalidate_recordset()
        purchase_line.invalidate_recordset()

        self.assertIn("sale_line", lock_order)
        self.assertIn("purchase", lock_order)
        self.assertLess(lock_order.index("sale_line"), lock_order.index("purchase"))
        self.assertEqual(component.purchase_line_id, purchase_line)
        self.assertEqual(purchase.order_line, purchase_line)
        self.assertAlmostEqual(purchase_line.product_qty, 4.0, places=2)

    def test_custom_door_reapply_cannot_diverge_confirmed_purchase_order(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        self.assertEqual(len(component), 1)
        self.assertEqual(purchase.state, "draft")
        self.assertEqual(purchase_line.name, "Vendor Lobby Door")
        self.assertAlmostEqual(purchase_line.price_unit, 40.0, places=2)

        # Draft RFQs remain editable and refresh their existing line in place.
        draft_edit = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        draft_edit.door_product_name = "Vendor Lobby Door Revised"
        draft_edit.door_vendor_sku = "VENDOR-DOOR-101"
        draft_edit.door_unit_cost = 45.0
        draft_edit.action_confirm()
        component.invalidate_recordset()
        purchase.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertEqual(component.purchase_line_id, purchase_line)
        self.assertEqual(purchase.order_line, purchase_line)
        self.assertEqual(component.name, "Vendor Lobby Door Revised")
        self.assertEqual(purchase_line.name, "Vendor Lobby Door Revised")
        self.assertAlmostEqual(component.quoted_unit_cost, 45.0, places=2)
        self.assertAlmostEqual(purchase_line.price_unit, 45.0, places=2)

        purchase.button_confirm()
        purchase.invalidate_recordset()
        self.assertNotEqual(purchase.state, "draft")

        # Reopening and applying unchanged data remains a valid no-op.
        no_op_snapshot = {
            "line_name": line.name,
            "component_name": component.name,
            "component_cost": component.quoted_unit_cost,
            "purchase_line_name": purchase_line.name,
            "purchase_line_price": purchase_line.price_unit,
        }
        no_op = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        no_op.action_confirm()
        line.invalidate_recordset()
        component.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertEqual(line.name, no_op_snapshot["line_name"])
        self.assertEqual(component.name, no_op_snapshot["component_name"])
        self.assertAlmostEqual(
            component.quoted_unit_cost,
            no_op_snapshot["component_cost"],
            places=2,
        )
        self.assertEqual(
            purchase_line.name,
            no_op_snapshot["purchase_line_name"],
        )
        self.assertAlmostEqual(
            purchase_line.price_unit,
            no_op_snapshot["purchase_line_price"],
            places=2,
        )

        # Customer-facing text may still change without changing the purchase.
        safe_edit = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        safe_edit.door_description = "Customer Lobby Door - Revised Label"
        safe_edit.special_notes = "Customer-facing clarification only."
        safe_edit.action_confirm()
        line.invalidate_recordset()
        self.assertEqual(
            line.cpq_special_description,
            "Customer Lobby Door - Revised Label",
        )
        self.assertEqual(component.name, no_op_snapshot["component_name"])
        self.assertEqual(
            purchase_line.name,
            no_op_snapshot["purchase_line_name"],
        )
        self.assertAlmostEqual(
            purchase_line.price_unit,
            no_op_snapshot["purchase_line_price"],
            places=2,
        )

        component_snapshot = {
            "name": component.name,
            "vendor_sku": component.vendor_sku,
            "quoted_unit_cost": component.quoted_unit_cost,
        }
        purchase_line_snapshot = {
            "name": purchase_line.name,
            "product_id": purchase_line.product_id,
            "product_qty": purchase_line.product_qty,
            "product_uom_id": purchase_line.product_uom_id,
            "price_unit": purchase_line.price_unit,
        }
        line_price_snapshot = (line.price_unit, line.purchase_price)
        header_quote_snapshot = line.cpq_special_vendor_quote_ref

        blocked_edit = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        blocked_edit.door_product_name = "Different Vendor Door"
        blocked_edit.door_vendor_sku = "VENDOR-DOOR-999"
        blocked_edit.door_unit_cost = 75.0
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            blocked_edit.action_confirm()

        blocked_header_edit = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        blocked_header_edit.vendor_quote_ref = "REVISED-AFTER-PO"
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            blocked_header_edit.action_confirm()

        add_component_vals = line._prepare_special_order_wizard_vals()
        add_component_vals["line_ids"] = [(0, 0, {
            "component_type": "inventory_item",
            "product_id": self.component_product_b.id,
            "name": "Late Added Hardware",
            "vendor_id": self.vendor_a.id,
            "product_uom_id": self.component_product_b.uom_id.id,
            "quantity": 1.0,
            "quoted_unit_cost": 15.0,
            "pricing_cost_source": "quote",
            "sourcing_method": "purchase",
        })]
        blocked_component_add = self.env["cpq.special.order.wizard"].create(
            add_component_vals
        )
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            blocked_component_add.action_confirm()

        new_order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
        })

        # Neither sudo, relationship detachment, reparenting, nor a parent SQL
        # cascade bypasses the purchase-state lock.
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            component.sudo().write({"quoted_unit_cost": 80.0})
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            component.sudo().write({
                "purchase_order_id": False,
                "purchase_line_id": False,
            })
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            component.sudo().write({
                "cpq_vendor_quote_number": "CHANGED-AFTER-PO",
                "cpq_vendor_quote_reference": "CHANGED-AFTER-PO",
                "cpq_vendor_quote_date": fields.Date.today(),
            })
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            line.sudo().write({
                "cpq_special_purchase_ids": [(5, 0, 0)],
            })
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            line.sudo().write({"order_id": new_order.id})

        with patch.object(
            type(line),
            "_ensure_cpq_special_bom_isolated",
            autospec=True,
        ) as isolate_bom, self.assertRaisesRegex(
            ValidationError,
            "is no longer a draft",
        ), self.env.cr.savepoint():
            self.env["sale.order.line.cpq.special.component"].create({
                "sale_line_id": line.id,
                "component_type": "inventory_item",
                "product_id": self.component_product_b.id,
                "name": "Late Direct Component",
                "vendor_id": self.vendor_a.id,
                "product_uom_id": self.component_product_b.uom_id.id,
                "quantity": 1.0,
                "quoted_unit_cost": 15.0,
                "pricing_cost_source": "quote",
                "sourcing_method": "purchase",
            })
        isolate_bom.assert_not_called()

        with patch.object(
            type(purchase),
            "lock_for_update",
            autospec=True,
            side_effect=LockError("Purchase row is busy"),
        ), self.assertRaisesRegex(
            ValidationError,
            "Purchasing is updating",
        ), self.env.cr.savepoint():
            component.sudo().write({"quoted_unit_cost": 81.0})

        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            component.sudo().unlink()

        protected_bom = self.env["mrp.bom"].create({
            "product_tmpl_id": line.product_template_id.id,
            "product_qty": 1.0,
            "product_uom_id": line.product_uom_id.id,
        })
        line._write_cpq_system_values({"cpq_bom_id": protected_bom.id})
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            line.sudo().unlink()
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            order.sudo().unlink()

        line.invalidate_recordset()
        component.invalidate_recordset()
        purchase.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertTrue(component.exists())
        self.assertTrue(order.exists())
        self.assertTrue(line.exists())
        self.assertTrue(protected_bom.exists())
        self.assertTrue(protected_bom.active)
        self.assertEqual(line.cpq_special_component_ids, component)
        self.assertEqual(component.name, component_snapshot["name"])
        self.assertEqual(component.vendor_sku, component_snapshot["vendor_sku"])
        self.assertAlmostEqual(
            component.quoted_unit_cost,
            component_snapshot["quoted_unit_cost"],
            places=2,
        )
        self.assertEqual(purchase.state, "purchase")
        self.assertEqual(purchase_line.name, purchase_line_snapshot["name"])
        self.assertEqual(purchase_line.product_id, purchase_line_snapshot["product_id"])
        self.assertAlmostEqual(
            purchase_line.product_qty,
            purchase_line_snapshot["product_qty"],
            places=2,
        )
        self.assertEqual(
            purchase_line.product_uom_id,
            purchase_line_snapshot["product_uom_id"],
        )
        self.assertAlmostEqual(
            purchase_line.price_unit,
            purchase_line_snapshot["price_unit"],
            places=2,
        )
        self.assertEqual((line.price_unit, line.purchase_price), line_price_snapshot)
        self.assertEqual(line.cpq_special_vendor_quote_ref, header_quote_snapshot)
        self.assertEqual(component.purchase_order_id, purchase)
        self.assertEqual(component.purchase_line_id, purchase_line)
        self.assertFalse(line.cpq_special_purchase_ids.filtered(lambda po: po.state == "draft"))

        # Internal defaulting must obey the same invariant even for legacy rows.
        self.env.cr.execute(
            """
                UPDATE sale_order_line_cpq_special_component
                   SET cpq_vendor_quote_currency_id = NULL
                 WHERE id = %s
            """,
            [component.id],
        )
        component.invalidate_recordset(["cpq_vendor_quote_currency_id"])
        self.assertFalse(component.cpq_vendor_quote_currency_id)
        with self.assertRaisesRegex(ValidationError, "is no longer a draft"), self.env.cr.savepoint():
            component._sync_default_values()
        component.invalidate_recordset(["cpq_vendor_quote_currency_id"])
        self.assertFalse(component.cpq_vendor_quote_currency_id)

    def test_custom_door_draft_rfq_is_cleaned_when_quote_is_deleted(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        self.assertEqual(purchase.state, "draft")

        order.unlink()

        self.assertFalse(order.exists())
        self.assertFalse(line.exists())
        self.assertFalse(component.exists())
        self.assertFalse(purchase.exists())
        self.assertFalse(purchase_line.exists())

    def test_native_quote_duplicate_isolates_custom_door_bom_and_confirmed_po(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        source_purchase_line = component.purchase_line_id
        source_bom = self.env["mrp.bom"].create({
            "product_tmpl_id": line.product_template_id.id,
            "product_id": line.product_id.id,
            "product_qty": 1.0,
            "product_uom_id": line.product_uom_id.id,
            "bom_line_ids": [(0, 0, {
                "product_id": component.product_id.id,
                "product_qty": component.quantity,
                "product_uom_id": component.product_uom_id.id,
                "cpq_special_component_id": component.id,
                "cpq_special_cost_source": component.pricing_cost_source,
                "cpq_special_unit_cost": component.quoted_unit_cost,
            })],
        })
        line._write_cpq_system_values({"cpq_bom_id": source_bom.id})
        line.write({"discount": 7.5})
        source_price = line.price_unit
        source_purchase_price = line.purchase_price

        purchase.button_confirm()
        purchase.invalidate_recordset()
        self.assertEqual(order.state, "draft")
        self.assertEqual(purchase.state, "purchase")

        copied_order = order.copy()
        copied_line = copied_order.order_line.filtered(
            lambda candidate: candidate._is_cpq_custom_door_line()
        )
        self.assertEqual(len(copied_line), 1)
        copied_component = copied_line.cpq_special_component_ids
        copied_bom = copied_line.cpq_bom_id

        self.assertEqual(copied_order.state, "draft")
        self.assertEqual(len(copied_component), 1)
        self.assertNotEqual(copied_component, component)
        self.assertFalse(copied_bom)
        self.assertFalse(copied_line.unico_duplicate_needs_configuration)
        self.assertEqual(
            source_bom.bom_line_ids.cpq_special_component_id,
            component,
        )
        self.assertFalse(copied_line.cpq_special_purchase_ids)
        self.assertFalse(copied_component.purchase_order_id)
        self.assertFalse(copied_component.purchase_line_id)
        self.assertAlmostEqual(copied_line.price_unit, source_price, places=2)
        self.assertAlmostEqual(
            copied_line.purchase_price,
            source_purchase_price,
            places=2,
        )
        self.assertAlmostEqual(copied_line.discount, 7.5, places=2)

        copied_wizard = self.env["cpq.special.order.wizard"].create(
            copied_line._prepare_special_order_wizard_vals()
        )
        copied_wizard.door_product_name = "Vendor Lobby Door Copy"
        copied_wizard.door_unit_cost = 55.0
        copied_wizard.action_confirm()

        line.invalidate_recordset()
        component.invalidate_recordset()
        copied_line.invalidate_recordset()
        copied_component.invalidate_recordset()
        purchase.invalidate_recordset()
        source_purchase_line.invalidate_recordset()
        self.assertEqual(line.cpq_bom_id, source_bom)
        self.assertTrue(source_bom.active)
        self.assertEqual(
            source_bom.bom_line_ids.cpq_special_component_id,
            component,
        )
        self.assertEqual(component.name, "Vendor Lobby Door")
        self.assertAlmostEqual(component.quoted_unit_cost, 40.0, places=2)
        self.assertEqual(component.purchase_order_id, purchase)
        self.assertEqual(component.purchase_line_id, source_purchase_line)
        self.assertAlmostEqual(line.purchase_price, source_purchase_price, places=2)
        self.assertEqual(purchase.state, "purchase")
        self.assertEqual(source_purchase_line.name, "Vendor Lobby Door")
        self.assertAlmostEqual(source_purchase_line.price_unit, 40.0, places=2)
        self.assertEqual(copied_component.name, "Vendor Lobby Door Copy")
        self.assertAlmostEqual(copied_component.quoted_unit_cost, 55.0, places=2)
        self.assertTrue(copied_component.purchase_order_id)
        self.assertTrue(copied_component.purchase_line_id)
        self.assertEqual(copied_component.purchase_order_id.state, "draft")
        self.assertNotEqual(copied_component.purchase_order_id, purchase)
        self.assertNotEqual(copied_component.purchase_line_id, source_purchase_line)
        self.assertFalse(copied_line.cpq_bom_id)

    def test_custom_door_component_with_confirmed_po_cannot_be_reparented(self):
        source_order, source_line, component = self._create_ready_special_line()
        self.assertFalse(source_line._is_cpq_custom_door_line())
        source_line._unico_auto_create_special_rfq()
        source_purchase = component.purchase_order_id
        source_purchase_line = component.purchase_line_id
        source_purchase.button_confirm()

        target_order, target_line, target_component, target_purchase = (
            self._create_custom_door_with_rfq()
        )

        with self.assertRaisesRegex(
            ValidationError,
            "is no longer a draft",
        ), self.env.cr.savepoint():
            component.write({"sale_line_id": target_line.id})

        component.invalidate_recordset()
        target_line.invalidate_recordset()
        source_purchase.invalidate_recordset()
        self.assertEqual(component.sale_line_id, source_line)
        self.assertEqual(source_line.cpq_special_component_ids, component)
        self.assertEqual(target_line.cpq_special_component_ids, target_component)
        self.assertEqual(component.purchase_order_id, source_purchase)
        self.assertEqual(component.purchase_line_id, source_purchase_line)
        self.assertEqual(source_purchase.state, "purchase")
        self.assertEqual(target_purchase.state, "draft")
        self.assertTrue(source_order.exists())
        self.assertTrue(target_order.exists())

    def test_first_custom_door_rfq_serializes_on_sale_line(self):
        _order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase.button_cancel()
        purchase.unlink()
        line.invalidate_recordset(["cpq_special_purchase_ids"])
        component.invalidate_recordset(["purchase_order_id", "purchase_line_id"])
        self.assertFalse(line.cpq_special_purchase_ids)
        self.assertFalse(component.purchase_order_id)
        self.assertFalse(component.purchase_line_id)

        with patch.object(
            type(line),
            "lock_for_update",
            autospec=True,
            side_effect=LockError("Sale line is busy"),
        ), self.assertRaisesRegex(
            ValidationError,
            "being updated by another process",
        ), self.env.cr.savepoint():
            line._unico_create_special_rfq(automation=False)

        line.invalidate_recordset(["cpq_special_purchase_ids"])
        component.invalidate_recordset(["purchase_order_id", "purchase_line_id"])
        self.assertFalse(line.cpq_special_purchase_ids)
        self.assertFalse(component.purchase_order_id)
        self.assertFalse(component.purchase_line_id)

    def test_line_delete_cleans_draft_rfq_when_m2m_link_is_stale(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        self.env.cr.execute(
            """
                DELETE FROM sale_order_line_cpq_special_purchase_rel
                 WHERE sale_line_id = %s
            """,
            [line.id],
        )
        line.invalidate_recordset(["cpq_special_purchase_ids"])
        self.assertFalse(line.cpq_special_purchase_ids)
        self.assertEqual(component.purchase_order_id, purchase)
        self.assertEqual(component.purchase_line_id, purchase_line)

        line.unlink()

        self.assertTrue(order.exists())
        self.assertFalse(line.exists())
        self.assertFalse(component.exists())
        self.assertFalse(purchase.exists())
        self.assertFalse(purchase_line.exists())

    def test_order_delete_cleans_draft_rfq_when_m2m_link_is_stale(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        self.env.cr.execute(
            """
                DELETE FROM sale_order_line_cpq_special_purchase_rel
                 WHERE sale_line_id = %s
            """,
            [line.id],
        )
        line.invalidate_recordset(["cpq_special_purchase_ids"])
        self.assertFalse(line.cpq_special_purchase_ids)
        self.assertEqual(component.purchase_order_id, purchase)

        order.unlink()

        self.assertFalse(order.exists())
        self.assertFalse(line.exists())
        self.assertFalse(component.exists())
        self.assertFalse(purchase.exists())
        self.assertFalse(purchase_line.exists())

    def test_sent_quote_delete_validates_before_special_rfq_cleanup(self):
        order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        bom = self.env["mrp.bom"].create({
            "product_tmpl_id": line.product_template_id.id,
            "product_qty": 1.0,
            "product_uom_id": line.product_uom_id.id,
        })
        line._write_cpq_system_values({"cpq_bom_id": bom.id})
        order.write({"state": "sent"})

        # Deliberately catch this without a savepoint.  No child mutation may
        # occur before native sale.order state validation raises.
        with self.assertRaisesRegex(UserError, "can not delete a sent quotation"):
            order.unlink()

        order.invalidate_recordset()
        line.invalidate_recordset()
        component.invalidate_recordset()
        purchase.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertTrue(order.exists())
        self.assertEqual(order.state, "sent")
        self.assertTrue(line.exists())
        self.assertTrue(component.exists())
        self.assertTrue(bom.exists())
        self.assertTrue(purchase.exists())
        self.assertTrue(purchase_line.exists())
        self.assertEqual(component.purchase_order_id, purchase)
        self.assertEqual(component.purchase_line_id, purchase_line)

    def test_custom_door_foreign_quote_currency_survives_reopen(self):
        foreign_currency = self.env.ref("base.EUR")
        foreign_currency.active = True
        today = fields.Date.today()
        rate = self.env["res.currency.rate"].search([
            ("currency_id", "=", foreign_currency.id),
            ("company_id", "=", self.env.company.id),
            ("name", "=", today),
        ], limit=1)
        rate_values = {
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
            "name": today,
            "rate": 0.5,
        }
        if rate:
            rate.write(rate_values)
        else:
            self.env["res.currency.rate"].create(rate_values)

        _order, line, component, purchase = self._create_custom_door_with_rfq()
        purchase_line = component.purchase_line_id
        component.write({
            "cpq_vendor_quote_currency_id": foreign_currency.id,
            "quoted_unit_cost": 40.0,
        })
        line._cpq_refresh_special_rfqs()
        purchase_line.invalidate_recordset()
        converted_purchase_price = purchase_line.price_unit

        ambiguous_edit = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        ambiguous_edit.door_unit_cost = 41.0
        with self.assertRaisesRegex(
            ValidationError,
            "vendor quote is in",
        ), self.env.cr.savepoint():
            ambiguous_edit.action_confirm()
        component.invalidate_recordset()
        self.assertEqual(component.cpq_vendor_quote_currency_id, foreign_currency)
        self.assertAlmostEqual(component.quoted_unit_cost, 40.0, places=2)

        purchase.button_confirm()
        reopened = self.env["cpq.special.order.wizard"].create(
            line._prepare_special_order_wizard_vals()
        )
        reopened.action_confirm()

        component.invalidate_recordset()
        purchase.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertEqual(component.cpq_vendor_quote_currency_id, foreign_currency)
        self.assertAlmostEqual(component.quoted_unit_cost, 40.0, places=2)
        self.assertEqual(component.purchase_line_id, purchase_line)
        self.assertEqual(purchase.state, "purchase")
        self.assertAlmostEqual(
            purchase_line.price_unit,
            converted_purchase_price,
            places=2,
        )

    def test_vendor_quote_currency_converts_to_purchase_currency(self):
        foreign_currency = self.env.ref("base.EUR")
        foreign_currency.active = True
        today = fields.Date.today()
        rate = self.env["res.currency.rate"].search([
            ("currency_id", "=", foreign_currency.id),
            ("company_id", "=", self.env.company.id),
            ("name", "=", today),
        ], limit=1)
        rate_values = {
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
            "name": today,
            "rate": 0.5,
        }
        if rate:
            rate.write(rate_values)
        else:
            self.env["res.currency.rate"].create(rate_values)

        _order, line, component = self._create_ready_special_line(cost=10.0)
        component.write({
            "cpq_vendor_quote_currency_id": foreign_currency.id,
            "quoted_unit_cost": 10.0,
        })
        line._cpq_refresh_special_rfqs()
        purchase = line.cpq_special_purchase_ids
        component.invalidate_recordset()

        expected_price = foreign_currency._convert(
            10.0,
            purchase.currency_id,
            purchase.company_id,
            fields.Date.to_date(purchase.date_order),
        )
        self.assertEqual(purchase.currency_id, self.env.company.currency_id)
        self.assertAlmostEqual(component.pricing_unit_cost, expected_price, places=2)
        self.assertAlmostEqual(purchase.order_line.price_unit, expected_price, places=2)

    def test_custom_product_confirmation_keeps_bom_mo_and_rfq_aligned(self):
        pricelist = self.env["product.pricelist"].create({
            "name": "Custom Product Cost Plus 20 Percent",
            "currency_id": self.env.company.currency_id.id,
        })
        self.env["product.pricelist.item"].create({
            "pricelist_id": pricelist.id,
            "applied_on": "3_global",
            "compute_price": "formula",
            "base": "standard_price",
            "price_discount": -20.0,
        })
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "pricelist_id": pricelist.id,
        })
        custom_product = self.env.ref(
            "klouded_cpq.product_tmpl_cpq_custom_product"
        ).product_variant_id
        line = self.env["sale.order.line"].create({
            "order_id": order.id,
            "product_id": custom_product.id,
            "product_uom_qty": 2.0,
            "cpq_special_workflow_type": "custom_product",
            "cpq_custom_product_category": "hardware",
            "cpq_custom_product_uom_id": custom_product.uom_id.id,
            "cpq_special_description": "Vendor-built custom hardware kit",
            "cpq_special_notes": "Procure from the approved quote and assemble.",
            "cpq_special_vendor_id": self.vendor_a.id,
            "cpq_custom_vendor_quote_number": "RFQ-CUSTOM-100",
            "cpq_special_vendor_quote_ref": "REF-CUSTOM-100",
            "cpq_special_vendor_quote_date": fields.Date.today(),
        })
        component = self.env["sale.order.line.cpq.special.component"].create({
            "sale_line_id": line.id,
            "component_type": "custom_product",
            "product_id": self.component_product.id,
            "name": "Quoted custom hardware component",
            "vendor_id": self.vendor_a.id,
            "vendor_sku": "VENDOR-CUSTOM-100",
            "product_uom_id": self.component_product.uom_id.id,
            "quantity": 4.0,
            "quoted_unit_cost": 25.0,
            "pricing_cost_source": "quote",
            "sourcing_method": "purchase",
            "cpq_vendor_quote_number": "RFQ-CUSTOM-100",
            "cpq_vendor_quote_reference": "REF-CUSTOM-100",
            "cpq_vendor_quote_date": fields.Date.today(),
        })

        line._generate_cpq_custom_product_bom()
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component.invalidate_recordset()

        purchase = line.cpq_special_purchase_ids
        purchase_line = component.purchase_line_id
        self.assertEqual(len(purchase), 1)
        self.assertEqual(purchase.partner_id, self.vendor_a)
        self.assertEqual(purchase_line.product_id, self.component_product)
        self.assertEqual(purchase_line.product_uom_id, self.component_product.uom_id)
        self.assertAlmostEqual(purchase_line.product_qty, 8.0, places=2)
        self.assertAlmostEqual(purchase_line.price_unit, 25.0, places=2)
        self.assertAlmostEqual(line.cpq_special_component_cost, 100.0, places=2)
        self.assertAlmostEqual(line.price_unit, 120.0, places=2)

        line.write({"product_uom_qty": 3.0})
        purchase_line.invalidate_recordset()
        self.assertAlmostEqual(purchase_line.product_qty, 12.0, places=2)

        original_bom = line.cpq_bom_id
        order.action_confirm()
        line.invalidate_recordset()

        self.assertEqual(order.state, "sale")
        self.assertEqual(line.cpq_bom_id, original_bom)
        self.assertEqual(len(line.cpq_production_ids), 1)
        self.assertEqual(line.cpq_production_ids.bom_id, original_bom)
        self.assertEqual(component.purchase_line_id, purchase_line)
        self.assertEqual(purchase.state, "draft")

        purchase.button_confirm()
        purchase.invalidate_recordset()
        with self.assertRaisesRegex(
            ValidationError,
            "is no longer a draft",
        ), self.env.cr.savepoint():
            line.write({"product_uom_qty": 4.0})
        line.invalidate_recordset()
        purchase_line.invalidate_recordset()
        self.assertAlmostEqual(line.product_uom_qty, 3.0, places=2)
        self.assertAlmostEqual(purchase_line.product_qty, 12.0, places=2)

        # Simulate a legacy issued PO created before sale-quantity scaling.
        self.env.cr.execute(
            "UPDATE purchase_order_line SET product_qty = %s WHERE id = %s",
            [component.quantity, purchase_line.id],
        )
        purchase_line.invalidate_recordset(["product_qty"])
        with self.assertRaisesRegex(
            ValidationError,
            "Issued RFQ/PO .* does not match",
        ):
            line._validate_cpq_custom_product_rfqs()
        self.assertAlmostEqual(purchase_line.product_qty, 4.0, places=2)

    def test_duplicate_special_line_keeps_source_rfq_and_creates_pending_demand(self):
        order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        source_purchase = line.cpq_special_purchase_ids
        source_purchase_lines = source_purchase.order_line
        original_lines = order.order_line

        line.action_unico_duplicate_line()
        duplicate = order.order_line - original_lines
        self.assertEqual(len(duplicate), 1)
        self.assertFalse(duplicate.cpq_special_purchase_ids)
        self.assertEqual(len(duplicate.cpq_special_component_ids), 1)
        self.assertNotEqual(duplicate.cpq_special_component_ids, component)
        self.assertFalse(duplicate.cpq_special_component_ids.purchase_order_id)
        self.assertFalse(duplicate.cpq_special_component_ids.purchase_line_id)
        self.assertEqual(duplicate.cpq_special_pending_purchase_count, 1)
        self.assertEqual(line.cpq_special_purchase_ids, source_purchase)
        self.assertEqual(source_purchase.order_line, source_purchase_lines)

        duplicate._unico_auto_create_special_rfq()
        duplicate.invalidate_recordset()
        copied_component = duplicate.cpq_special_component_ids
        self.assertEqual(len(duplicate.cpq_special_purchase_ids), 1)
        self.assertNotEqual(duplicate.cpq_special_purchase_ids, source_purchase)
        self.assertEqual(
            copied_component.purchase_line_id.unico_cpq_special_sale_line_id,
            duplicate,
        )
        self.assertEqual(line.cpq_special_purchase_ids, source_purchase)
        self.assertEqual(component.purchase_line_id, source_purchase_lines)

    def test_auto_special_rfq_refreshes_existing_draft_in_place(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        old_purchase_line = purchase.order_line

        component.write({
            "name": "Updated Vendor Component",
            "quantity": 4.0,
            "quoted_unit_cost": 42.0,
        })
        line._unico_auto_create_special_rfq()
        purchase.invalidate_recordset()
        component.invalidate_recordset()

        self.assertEqual(line.cpq_special_purchase_ids, purchase)
        self.assertEqual(len(purchase.order_line), 1)
        self.assertEqual(purchase.order_line, old_purchase_line)
        self.assertEqual(purchase.order_line.name, "Updated Vendor Component")
        self.assertAlmostEqual(purchase.order_line.product_qty, 4.0, places=2)
        self.assertAlmostEqual(purchase.order_line.price_unit, 42.0, places=2)
        self.assertEqual(component.purchase_line_id, purchase.order_line)

    def test_auto_special_rfq_reuses_linked_manual_draft_instead_of_duplication(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        manual_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [
                (0, 0, {
                    "name": component.name,
                    "product_id": self.component_product.id,
                    "product_qty": 2.0,
                    "product_uom_id": self.component_product.uom_id.id,
                    "price_unit": 25.0,
                    "date_planned": fields.Datetime.now(),
                }),
                (0, 0, {
                    "name": "Manual freight line",
                    "product_qty": 1.0,
                    "price_unit": 75.0,
                    "date_planned": fields.Datetime.now(),
                }),
            ],
        })
        component_line = manual_purchase.order_line.filtered(lambda po_line: po_line.product_id == self.component_product)
        manual_extra_line = manual_purchase.order_line.filtered(lambda po_line: po_line.name == "Manual freight line")
        component.write({
            "purchase_order_id": manual_purchase.id,
            "purchase_line_id": component_line.id,
        })
        line.cpq_special_purchase_ids = [(4, manual_purchase.id)]

        component.write({
            "quantity": 5.0,
            "quoted_unit_cost": 55.0,
        })
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        manual_purchase.invalidate_recordset()
        component.invalidate_recordset()

        self.assertEqual(line.cpq_special_purchase_ids, manual_purchase)
        self.assertFalse(manual_purchase.unico_auto_special_rfq)
        self.assertIn(manual_extra_line, manual_purchase.order_line)
        self.assertEqual(len(manual_purchase.order_line), 2)
        self.assertAlmostEqual(component_line.product_qty, 5.0, places=2)
        self.assertAlmostEqual(component_line.price_unit, 55.0, places=2)
        self.assertEqual(component.purchase_line_id, component_line)

    def test_auto_special_rfq_orders_current_qty_beside_unmarked_legacy_line(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        legacy_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [(0, 0, {
                "name": component.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        legacy_line = legacy_purchase.order_line
        component.write({
            "purchase_order_id": legacy_purchase.id,
            "purchase_line_id": False,
            "quantity": 6.0,
            "quoted_unit_cost": 66.0,
        })
        line.cpq_special_purchase_ids = [(4, legacy_purchase.id)]
        before_messages = line.order_id.message_ids

        line._unico_auto_create_special_rfq()
        legacy_purchase.invalidate_recordset()
        component.invalidate_recordset()

        # The unmarked legacy line is never adopted or edited...
        self.assertFalse(legacy_line.unico_cpq_special_sale_line_id)
        self.assertAlmostEqual(legacy_line.product_qty, 2.0, places=2)
        self.assertAlmostEqual(legacy_line.price_unit, 25.0, places=2)
        # ...but the component is still ordered, at its current qty/price, on a
        # new automation-owned line beside the untouched legacy line.
        new_line = component.purchase_line_id
        self.assertTrue(new_line)
        self.assertNotEqual(new_line, legacy_line)
        self.assertEqual(new_line.unico_cpq_special_sale_line_id, line)
        self.assertAlmostEqual(new_line.product_qty, 6.0, places=2)
        self.assertAlmostEqual(new_line.price_unit, 66.0, places=2)
        self.assertEqual(len(legacy_purchase.order_line), 2)
        new_messages = line.order_id.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("possible duplicates", new_messages.body)

    def test_auto_special_rfq_orders_current_qty_beside_unmarked_buyer_line(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        legacy_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [(0, 0, {
                "name": "Buyer-added same product line",
                "product_id": self.component_product.id,
                "product_qty": 1.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 9.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        manual_line = legacy_purchase.order_line
        component.write({
            "purchase_order_id": legacy_purchase.id,
            "purchase_line_id": False,
            "quantity": 6.0,
            "quoted_unit_cost": 66.0,
        })
        line.cpq_special_purchase_ids = [(4, legacy_purchase.id)]
        before_messages = line.order_id.message_ids

        line._unico_auto_create_special_rfq()
        legacy_purchase.invalidate_recordset()
        component.invalidate_recordset()

        # The buyer's manual line is preserved exactly...
        self.assertFalse(manual_line.unico_cpq_special_sale_line_id)
        self.assertEqual(manual_line.name, "Buyer-added same product line")
        self.assertAlmostEqual(manual_line.product_qty, 1.0, places=2)
        self.assertAlmostEqual(manual_line.price_unit, 9.0, places=2)
        # ...and the component is ordered on a separate automation-owned line.
        new_line = component.purchase_line_id
        self.assertTrue(new_line)
        self.assertNotEqual(new_line, manual_line)
        self.assertEqual(new_line.unico_cpq_special_sale_line_id, line)
        self.assertAlmostEqual(new_line.product_qty, 6.0, places=2)
        self.assertAlmostEqual(new_line.price_unit, 66.0, places=2)
        self.assertEqual(len(legacy_purchase.order_line), 2)
        new_messages = line.order_id.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("possible duplicates", new_messages.body)

    def test_manual_special_rfq_refresh_marks_auto_draft_as_manually_maintained(self):
        _order, line, _component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        self.assertTrue(purchase.unico_auto_special_rfq)

        line._unico_create_special_rfq(automation=False)
        purchase.invalidate_recordset()

        self.assertFalse(purchase.unico_auto_special_rfq)

    def test_auto_special_rfq_removes_deleted_component_line_but_keeps_manual_lines(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        removed_component = self.env["sale.order.line.cpq.special.component"].create({
            "sale_line_id": line.id,
            "component_type": "inventory_item",
            "product_id": self.component_product_b.id,
            "name": "Vendor Special Hardware Component",
            "vendor_id": self.vendor_a.id,
            "product_uom_id": self.component_product_b.uom_id.id,
            "quantity": 3.0,
            "quoted_unit_cost": 15.0,
            "pricing_cost_source": "quote",
            "sourcing_method": "purchase",
        })
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        manual_line = self.env["purchase.order.line"].create({
            "order_id": purchase.id,
            "name": "Manual freight line",
            "product_qty": 1.0,
            "price_unit": 75.0,
            "date_planned": fields.Datetime.now(),
        })

        removed_component.unlink()
        line._unico_auto_create_special_rfq()
        purchase.invalidate_recordset()
        component.invalidate_recordset()

        self.assertTrue(purchase.exists())
        self.assertIn(manual_line, purchase.order_line)
        self.assertIn(component.purchase_line_id, purchase.order_line)
        self.assertFalse(purchase.order_line.filtered(lambda po_line: po_line.product_id == self.component_product_b))
        self.assertEqual(len(purchase.order_line), 2)

    def test_auto_special_rfq_cleans_up_draft_when_all_components_are_removed(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids

        component.unlink()
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()

        # No purchasable demand remains and the draft was purely automation-owned,
        # so it is cancelled and removed rather than left confirmable by purchasing.
        self.assertFalse(purchase.exists())
        self.assertFalse(line.cpq_special_purchase_ids)

    def test_auto_special_rfq_strips_owned_lines_but_keeps_buyer_lines_when_components_removed(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        owned_line = purchase.order_line
        buyer_line = self.env["purchase.order.line"].create({
            "order_id": purchase.id,
            "name": "Buyer freight line",
            "product_qty": 1.0,
            "price_unit": 50.0,
            "date_planned": fields.Datetime.now(),
        })
        before_messages = line.order_id.message_ids

        component.unlink()
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        purchase.invalidate_recordset()

        # The RFQ survives because it still carries a buyer line, but our owned
        # line is removed and the RFQ is unlinked from the sale line, so no stale
        # automation demand can be ordered for it.
        self.assertTrue(purchase.exists())
        self.assertFalse(owned_line.exists())
        self.assertIn(buyer_line, purchase.order_line)
        self.assertNotIn(purchase, line.cpq_special_purchase_ids)
        new_messages = line.order_id.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("No purchasable CPQ special components remain", new_messages.body)

    def test_component_unlink_releases_owned_rfq_demand(self):
        _order, line, component = self._create_ready_special_line(quantity=2.0, cost=25.0)
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        self.assertTrue(purchase.exists())

        component.unlink()
        line.invalidate_recordset()

        # Removing the component reconciles the special RFQ on the spot (no
        # second save/pass needed): the purely-automation-owned draft is
        # cancelled and removed, so purchasing has nothing stale left to confirm.
        self.assertFalse(purchase.exists())
        self.assertFalse(line.cpq_special_purchase_ids)

    def test_auto_special_rfq_replaces_stale_draft_after_vendor_change(self):
        _order, line, component = self._create_ready_special_line(vendor=self.vendor_a)
        line._unico_auto_create_special_rfq()
        stale_purchase = line.cpq_special_purchase_ids

        component.write({"vendor_id": self.vendor_b.id})
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component.invalidate_recordset()

        self.assertFalse(stale_purchase.exists())
        purchase = line.cpq_special_purchase_ids
        self.assertEqual(len(purchase), 1)
        self.assertEqual(purchase.partner_id, self.vendor_b)
        self.assertEqual(component.purchase_order_id, purchase)

    @mute_logger(_RFQ_LOGGER)
    def test_auto_special_rfq_orders_new_vendor_and_keeps_buyer_lines_on_stale_draft(self):
        _order, line, component = self._create_ready_special_line(vendor=self.vendor_a)
        manual_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [
                (0, 0, {
                    "name": component.name,
                    "product_id": self.component_product.id,
                    "product_qty": 2.0,
                    "product_uom_id": self.component_product.uom_id.id,
                    "price_unit": 25.0,
                    "date_planned": fields.Datetime.now(),
                }),
                (0, 0, {
                    "name": "Buyer freight line",
                    "product_qty": 1.0,
                    "price_unit": 50.0,
                    "date_planned": fields.Datetime.now(),
                }),
            ],
        })
        component_line = manual_purchase.order_line.filtered(
            lambda po_line: po_line.product_id == self.component_product
        )
        freight_line = manual_purchase.order_line.filtered(
            lambda po_line: po_line.name == "Buyer freight line"
        )
        component.write({
            "purchase_order_id": manual_purchase.id,
            "purchase_line_id": component_line.id,
        })
        line.cpq_special_purchase_ids = [(4, manual_purchase.id)]
        before_messages = line.order_id.message_ids

        component.write({"vendor_id": self.vendor_b.id})
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component.invalidate_recordset()

        # The new vendor's RFQ is created and now owns the component...
        new_purchase = line.cpq_special_purchase_ids
        self.assertEqual(len(new_purchase), 1)
        self.assertEqual(new_purchase.partner_id, self.vendor_b)
        self.assertEqual(component.purchase_order_id, new_purchase)
        self.assertNotEqual(new_purchase, manual_purchase)
        # ...while the stale vendor-A draft keeps its buyer freight line but our
        # component line is removed (so it cannot double-order), and the draft is
        # released from the sale line for purchasing to review.
        self.assertTrue(manual_purchase.exists())
        self.assertFalse(component_line.exists())
        self.assertIn(freight_line, manual_purchase.order_line)
        self.assertNotIn(manual_purchase, line.cpq_special_purchase_ids)
        new_messages = line.order_id.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("left for purchasing to review", new_messages.body)

    @mute_logger(_RFQ_LOGGER)
    def test_auto_special_rfq_orders_new_vendor_and_keeps_shared_draft_for_other_line(self):
        _order_1, line_1, component_1 = self._create_ready_special_line(vendor=self.vendor_a)
        _order_2, line_2, component_2 = self._create_ready_special_line(vendor=self.vendor_a)
        manual_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "Shared manual special RFQ",
            "order_line": [(0, 0, {
                "name": component_1.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        component_1.write({
            "purchase_order_id": manual_purchase.id,
            "purchase_line_id": manual_purchase.order_line.id,
        })
        component_2.write({
            "purchase_order_id": manual_purchase.id,
            "purchase_line_id": manual_purchase.order_line.id,
        })
        line_1.cpq_special_purchase_ids = [(4, manual_purchase.id)]
        line_2.cpq_special_purchase_ids = [(4, manual_purchase.id)]
        before_messages = line_1.order_id.message_ids

        component_1.write({"vendor_id": self.vendor_b.id})
        line_1._unico_auto_create_special_rfq()
        line_1.invalidate_recordset()
        line_2.invalidate_recordset()
        component_1.invalidate_recordset()

        # line_1 gets its own new vendor-B RFQ...
        new_purchase = line_1.cpq_special_purchase_ids
        self.assertEqual(new_purchase.partner_id, self.vendor_b)
        self.assertEqual(component_1.purchase_order_id, new_purchase)
        # ...and the shared draft is left untouched, still serving line_2.
        self.assertTrue(manual_purchase.exists())
        self.assertNotIn(manual_purchase, line_1.cpq_special_purchase_ids)
        self.assertIn(manual_purchase, line_2.cpq_special_purchase_ids)
        self.assertEqual(len(manual_purchase.order_line), 1)
        new_messages = line_1.order_id.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("left for purchasing to review", new_messages.body)

    @mute_logger(_RFQ_LOGGER)
    def test_auto_special_rfq_orders_new_vendor_even_when_stale_cleanup_fails(self):
        order, line, component = self._create_ready_special_line(vendor=self.vendor_a)
        line._unico_auto_create_special_rfq()
        stale_purchase = line.cpq_special_purchase_ids
        before_messages = order.message_ids

        component.write({"vendor_id": self.vendor_b.id})
        with patch.object(
            type(stale_purchase),
            "button_cancel",
            autospec=True,
            side_effect=UserError("Forced cleanup failure"),
        ):
            line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        stale_purchase.invalidate_recordset()
        component.invalidate_recordset()

        # The new vendor's RFQ is created and owns the component...
        new_purchase = line.cpq_special_purchase_ids
        self.assertEqual(len(new_purchase), 1)
        self.assertEqual(new_purchase.partner_id, self.vendor_b)
        self.assertEqual(component.purchase_order_id, new_purchase)
        # ...and the un-cancellable stale draft survives but no longer carries
        # our line, so it cannot double-order, and it is released from the line.
        self.assertTrue(stale_purchase.exists())
        self.assertNotIn(stale_purchase, line.cpq_special_purchase_ids)
        self.assertFalse(stale_purchase.order_line.filtered(
            lambda po_line: po_line.unico_cpq_special_sale_line_id == line
        ))
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("left for purchasing to review", new_messages.body)

    @mute_logger(_RFQ_LOGGER)
    def test_manual_special_rfq_orders_new_vendor_even_when_stale_cleanup_fails(self):
        _order, line, component = self._create_ready_special_line(vendor=self.vendor_a)
        line._unico_auto_create_special_rfq()
        stale_purchase = line.cpq_special_purchase_ids

        component.write({"vendor_id": self.vendor_b.id})
        with patch.object(
            type(stale_purchase),
            "button_cancel",
            autospec=True,
            side_effect=UserError("Forced cleanup failure"),
        ):
            line._unico_create_special_rfq(automation=False)
        line.invalidate_recordset()
        component.invalidate_recordset()

        self.assertTrue(stale_purchase.exists())
        new_purchase = line.cpq_special_purchase_ids
        self.assertEqual(new_purchase.partner_id, self.vendor_b)
        self.assertEqual(component.purchase_order_id, new_purchase)
        self.assertNotIn(stale_purchase, line.cpq_special_purchase_ids)

    def test_unlink_keeps_linked_manual_draft_rfq_for_purchasing_review(self):
        order, line, component = self._create_ready_special_line()
        manual_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [(0, 0, {
                "name": component.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        component.write({
            "purchase_order_id": manual_purchase.id,
            "purchase_line_id": manual_purchase.order_line.id,
        })
        line.cpq_special_purchase_ids = [(4, manual_purchase.id)]
        before_messages = order.message_ids

        line.unlink()

        self.assertFalse(line.exists())
        self.assertTrue(manual_purchase.exists())
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("contains manual or unverified purchase lines", new_messages.body)

    @mute_logger(_RFQ_LOGGER)
    def test_unlink_does_not_block_when_linked_manual_draft_rfq_cleanup_fails(self):
        order, line, _component = self._create_ready_special_line()
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids
        before_messages = order.message_ids

        with patch.object(
            type(purchase),
            "button_cancel",
            autospec=True,
            side_effect=UserError("Forced cleanup failure"),
        ):
            line.unlink()

        self.assertFalse(line.exists())
        self.assertTrue(purchase.exists())
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("cleanup could not remove stale draft RFQ", new_messages.body)

    def test_unlink_cleans_auto_rfq_using_predelete_ownership_snapshot(self):
        _order, line, _component = self._create_ready_special_line()
        line._unico_auto_create_special_rfq()
        purchase = line.cpq_special_purchase_ids

        original_cleanup = type(line)._unico_cleanup_draft_special_rfqs

        def invalidate_then_cleanup(record, purchases, *args, **kwargs):
            record.env.invalidate_all()
            return original_cleanup(record, purchases, *args, **kwargs)

        with patch.object(
            type(line),
            "_unico_cleanup_draft_special_rfqs",
            autospec=True,
            side_effect=invalidate_then_cleanup,
        ):
            line.unlink()

        self.env.invalidate_all()
        self.assertFalse(line.exists())
        self.assertFalse(purchase.exists())

    @mute_logger(_RFQ_LOGGER)
    def test_batch_unlink_posts_cleanup_failure_on_each_source_order(self):
        order_a, line_a, component_a = self._create_ready_special_line()
        order_b, line_b, component_b = self._create_ready_special_line()
        purchase_a = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line_a.order_id.name, line_a.name),
            "order_line": [(0, 0, {
                "name": component_a.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        purchase_b = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line_b.order_id.name, line_b.name),
            "order_line": [(0, 0, {
                "name": component_b.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        component_a.write({
            "purchase_order_id": purchase_a.id,
            "purchase_line_id": purchase_a.order_line.id,
        })
        component_b.write({
            "purchase_order_id": purchase_b.id,
            "purchase_line_id": purchase_b.order_line.id,
        })
        line_a.cpq_special_purchase_ids = [(4, purchase_a.id)]
        line_b.cpq_special_purchase_ids = [(4, purchase_b.id)]
        before_a = order_a.message_ids
        before_b = order_b.message_ids

        with patch.object(
            type(purchase_a | purchase_b),
            "button_cancel",
            autospec=True,
            side_effect=UserError("Forced cleanup failure"),
        ):
            (line_a | line_b).unlink()

        self.assertEqual(len(order_a.message_ids - before_a), 1)
        self.assertEqual(len(order_b.message_ids - before_b), 1)
        self.assertIn("cleanup could not remove stale draft RFQ", (order_a.message_ids - before_a).body)
        self.assertIn("cleanup could not remove stale draft RFQ", (order_b.message_ids - before_b).body)

    def test_multi_vendor_creates_missing_vendor_rfq_beside_existing_draft(self):
        order, line, component_a = self._create_ready_special_line(
            vendor=self.vendor_a,
            quantity=2.0,
            cost=25.0,
        )
        component_b = self.env["sale.order.line.cpq.special.component"].create({
            "sale_line_id": line.id,
            "component_type": "inventory_item",
            "product_id": self.component_product_b.id,
            "name": "Vendor Special Hardware Component",
            "vendor_id": self.vendor_b.id,
            "product_uom_id": self.component_product_b.uom_id.id,
            "quantity": 3.0,
            "quoted_unit_cost": 15.0,
            "pricing_cost_source": "quote",
            "sourcing_method": "purchase",
        })
        vendor_b_purchase = self.env["purchase.order"].create({
            "partner_id": self.vendor_b.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [(0, 0, {
                "name": component_b.name,
                "product_id": self.component_product_b.id,
                "product_qty": 3.0,
                "product_uom_id": self.component_product_b.uom_id.id,
                "price_unit": 15.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        line.cpq_special_purchase_ids = [(4, vendor_b_purchase.id)]
        before_messages = order.message_ids

        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component_a.invalidate_recordset()
        component_b.invalidate_recordset()
        vendor_a_purchases = self.env["purchase.order"].search([
            ("partner_id", "=", self.vendor_a.id),
            ("origin", "ilike", order.name),
        ])

        # Vendor A's RFQ is created (never orphaned) and owns component A.
        self.assertEqual(len(vendor_a_purchases), 1)
        self.assertEqual(component_a.purchase_order_id, vendor_a_purchases)
        self.assertAlmostEqual(component_a.purchase_line_id.product_qty, 2.0, places=2)
        # Component B is ordered on a new owned line beside the pre-existing
        # unmarked line, which is left for review.
        self.assertEqual(component_b.purchase_order_id, vendor_b_purchase)
        self.assertTrue(component_b.purchase_line_id)
        self.assertEqual(component_b.purchase_line_id.unico_cpq_special_sale_line_id, line)
        self.assertEqual(len(vendor_b_purchase.order_line), 2)
        self.assertIn(vendor_a_purchases, line.cpq_special_purchase_ids)
        self.assertIn(vendor_b_purchase, line.cpq_special_purchase_ids)
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("possible duplicates", new_messages.body)

    def test_duplicate_same_vendor_draft_is_released_not_double_ordered(self):
        _order, line, component = self._create_ready_special_line(
            vendor=self.vendor_a, quantity=2.0, cost=25.0,
        )
        line._unico_auto_create_special_rfq()
        first_draft = line.cpq_special_purchase_ids
        # A second draft for the same vendor that also carries a marked line for
        # this component (e.g. an earlier run that picked a different draft).
        second_draft = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "origin": "%s / %s" % (line.order_id.name, line.name),
            "order_line": [(0, 0, {
                "name": component.name,
                "product_id": self.component_product.id,
                "product_qty": 2.0,
                "product_uom_id": self.component_product.uom_id.id,
                "price_unit": 25.0,
                "date_planned": fields.Datetime.now(),
                "unico_cpq_special_sale_line_id": line.id,
            })],
        })
        line.cpq_special_purchase_ids = [(4, second_draft.id)]

        component.write({"quantity": 5.0})
        line._unico_auto_create_special_rfq()
        line.invalidate_recordset()
        component.invalidate_recordset()

        # Whichever draft is chosen, the component's material now appears on
        # exactly one purchase line at the current quantity (no double-order),
        # and only one draft stays linked to the sale line.
        remaining = (first_draft + second_draft).exists()
        component_lines = remaining.order_line.filtered(
            lambda po_line: po_line.product_id == self.component_product
        )
        self.assertEqual(len(component_lines), 1)
        self.assertEqual(component.purchase_line_id, component_lines)
        self.assertEqual(component_lines.unico_cpq_special_sale_line_id, line)
        self.assertAlmostEqual(component_lines.product_qty, 5.0, places=2)
        self.assertEqual(len(line.cpq_special_purchase_ids), 1)

    @mute_logger(_RFQ_LOGGER)
    def test_auto_special_rfq_failure_posts_visible_sale_order_note(self):
        order, line, _component = self._create_ready_special_line()
        before_messages = order.message_ids

        with patch.object(
            type(line),
            "_unico_create_special_rfq",
            autospec=True,
            side_effect=UserError("Forced RFQ failure"),
        ):
            line._unico_auto_create_special_rfq()

        order.invalidate_recordset()
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("Automatic special RFQ creation failed", new_messages.body)
        self.assertIn("purchasing still needs to create the RFQ manually", new_messages.body)
        self.assertIn("Forced RFQ failure", new_messages.body)
        self.assertFalse(line.cpq_special_purchase_ids)

    @mute_logger(_RFQ_LOGGER)
    def test_auto_special_rfq_unexpected_failure_still_posts_visible_note(self):
        order, line, _component = self._create_ready_special_line()
        before_messages = order.message_ids

        with patch.object(
            type(line),
            "_unico_create_special_rfq",
            autospec=True,
            side_effect=RuntimeError("Forced unexpected RFQ failure"),
        ):
            line._unico_auto_create_special_rfq()

        order.invalidate_recordset()
        new_messages = order.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        self.assertIn("Automatic special RFQ creation failed", new_messages.body)
        self.assertIn("Forced unexpected RFQ failure", new_messages.body)
