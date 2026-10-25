from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import formatLang

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestRmaLines(RmaTestCommon):

    def _create_bom(
        self,
        product,
        component,
        component_qty,
        *,
        bom_type="normal",
        sequence=10,
    ):
        return self.env["mrp.bom"].create({
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "product_qty": 1.0,
            "product_uom_id": product.uom_id.id,
            "type": bom_type,
            "sequence": sequence,
            "bom_line_ids": [(0, 0, {
                "product_id": component.id,
                "product_qty": component_qty,
                "product_uom_id": component.uom_id.id,
            })],
        })

    def _make_sale_return_line(
        self,
        sale_line,
        *,
        return_qty=2.0,
        delivery_qty=5.0,
        delivery_order=False,
    ):
        rma = self._make_rma(
            rma_type="rma_with_do",
            delivery_order=delivery_order.id if delivery_order else False,
            lines=[{
                "product_id": sale_line.product_id.id,
                "return_qty": return_qty,
                "price_unit": sale_line.price_unit,
                "delivery_qty": delivery_qty,
                "sale_line_id": sale_line.id,
            }],
        )
        return rma.rma_line_ids

    def test_total_price_compute(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 4,
            "price_unit": 25.0,
            "delivery_qty": 5.0,
        }])
        line = rma.rma_line_ids
        self.assertEqual(line.total_price, 4 * 25.0)

    def test_total_price_recomputes_on_return_qty_change(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 1,
            "price_unit": 25.0,
            "delivery_qty": 5.0,
        }])
        line = rma.rma_line_ids
        line.return_qty = 3
        line._update_total_price()
        self.assertEqual(line.total_price, 3 * 25.0)

    def test_return_qty_preserves_fractional_quantities(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 2.5,
            "price_unit": 10.0,
            "delivery_qty": 5.0,
        }])
        line = rma.rma_line_ids
        self.assertEqual(line.return_qty, 2.5)
        self.assertEqual(line.total_price, 25.0)

    def test_onchange_return_qty_raises_when_over_delivered(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 2.0,
        }])
        line = rma.rma_line_ids
        line.return_qty = 5
        with self.assertRaises(UserError):
            line._onchange_return_qty()

    def test_onchange_return_qty_ok_when_within_delivered(self):
        rma = self._make_rma(lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        line = rma.rma_line_ids
        line.return_qty = 3
        # Should not raise
        line._onchange_return_qty()

    def test_finished_good_uses_full_sale_description_and_scaled_bom(self):
        sale_line = self.sale_order.order_line
        sale_line.name = (
            "[CPQ-CUSTOM-PRODUCT] Custom Product\n"
            "Special Order Items: 1 PC - Chinedu molding"
        )
        self._create_bom(
            sale_line.product_id,
            self.product_b,
            2.0,
        )

        rma_line = self._make_sale_return_line(sale_line)
        expected_qty = formatLang(self.env, 4.0, dp="Product Unit")

        self.assertEqual(rma_line.original_description, sale_line.name)
        self.assertEqual(rma_line.return_scope, "finished_good")
        self.assertEqual(rma_line.return_scope_label, "Finished Good")
        self.assertIn(expected_qty, rma_line.return_component_summary)
        self.assertIn(self.product_b.display_name, rma_line.return_component_summary)

    def test_custom_product_uses_actual_cpq_configuration_details_everywhere(self):
        if "cpq.custom.product.wizard" not in self.env.registry.models:
            self.skipTest("klouded_cpq is not installed")
        custom_product_template = self.env.ref(
            "klouded_cpq.product_tmpl_cpq_custom_product",
            raise_if_not_found=False,
        )
        if not custom_product_template:
            self.skipTest("Custom Product placeholder is not installed")

        custom_product = custom_product_template.product_variant_id
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
        })
        sale_line = self.env["sale.order.line"].create({
            "order_id": order.id,
            "product_id": custom_product.id,
            "product_uom_qty": 1.0,
        })
        wizard = self.env["cpq.custom.product.wizard"].create({
            "order_line_id": sale_line.id,
            "item_category": "molding",
            "product_description": "Custom walnut trim assembly",
            "notes": "Return identification must preserve the configured scope.",
            "product_uom_id": custom_product.uom_id.id,
            "line_ids": [(0, 0, {
                "source_type": "inventory",
                "product_id": self.product_b.id,
                "description": "Walnut return molding",
                "product_uom_id": self.product_b.uom_id.id,
                "quantity": 2.0,
            })],
        })
        wizard.action_confirm()
        sale_line.invalidate_recordset()

        configuration_details = sale_line.cpq_configuration_description
        self.assertTrue(configuration_details)
        self.assertIn("Walnut return molding", configuration_details)

        rma = self._make_rma(
            rma_type="rma_with_do",
            sale_order=order.id,
            lines=[{
                "product_id": custom_product.id,
                "return_qty": 1.0,
                "price_unit": sale_line.price_unit,
                "delivery_qty": 1.0,
                "sale_line_id": sale_line.id,
            }],
        )
        rma_line = rma.rma_line_ids
        self.assertEqual(
            rma_line.original_description,
            "%s\n%s" % (sale_line.name, configuration_details),
        )

        return_wizard = (
            self.env["return.order"]
            .with_context(
                default_rma_id=rma.id,
                default_rma_type=rma.rma_type,
                default_rma_line=rma_line.ids,
            )
            .create({})
        )
        self.assertEqual(
            return_wizard.rol_ids.original_description,
            rma_line.original_description,
        )

        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "unico_rma.rma_report",
            [rma.id],
        )
        rendered = html.decode()
        self.assertIn("Custom walnut trim assembly", rendered)
        self.assertIn("Walnut return molding", rendered)

    def test_configured_door_description_includes_configuration_details(self):
        sale_line = self.sale_order.order_line
        if "cpq_configuration_description" not in sale_line._fields:
            self.skipTest("klouded_cpq is not installed")

        sale_line.name = "Configure Door - Rockport"
        configuration_details = (
            "RESINT DBL RP 2-4, 6-8, 1-3/8, HC, NFR, FJ, SN, BLK"
        )
        sale_line.cpq_configuration_description = configuration_details

        rma_line = self._make_sale_return_line(sale_line)

        self.assertEqual(
            rma_line.original_description,
            "%s\n%s" % (sale_line.name, configuration_details),
        )

        updated_details = "%s, updated handing" % configuration_details
        sale_line.cpq_configuration_description = updated_details
        self.assertEqual(
            rma_line.original_description,
            "%s\n%s" % (sale_line.name, updated_details),
        )

    def test_phantom_bom_is_identified_as_component_return(self):
        kit = self.env["product.product"].create({
            "name": "Configured Door Kit",
            "type": "consu",
            "list_price": 500.0,
        })
        sale_line = self.env["sale.order.line"].create({
            "order_id": self.sale_order.id,
            "product_id": kit.id,
            "product_uom_qty": 3.0,
            "price_unit": 500.0,
            "name": "Configured Door Kit\nRockport, black, double door",
        })
        self._create_bom(
            kit,
            self.product_b,
            3.0,
            bom_type="phantom",
        )

        rma_line = self._make_sale_return_line(
            sale_line,
            return_qty=2.0,
            delivery_qty=3.0,
        )
        expected_qty = formatLang(self.env, 6.0, dp="Product Unit")

        self.assertEqual(rma_line.return_scope, "components")
        self.assertEqual(rma_line.return_scope_label, "Components")
        self.assertIn(expected_qty, rma_line.return_component_summary)

    def test_component_delivery_moves_control_partial_return_breakdown(self):
        sale_line = self.sale_order.order_line
        warehouse = self.sale_order.warehouse_id
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": warehouse.out_type_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
        })
        self.env["stock.move"].create({
            "product_id": self.product_b.id,
            "product_uom_qty": 10.0,
            "product_uom": self.product_b.uom_id.id,
            "location_id": picking.location_id.id,
            "location_dest_id": picking.location_dest_id.id,
            "picking_id": picking.id,
            "sale_line_id": sale_line.id,
        })

        rma_line = self._make_sale_return_line(
            sale_line,
            return_qty=2.0,
            delivery_qty=5.0,
            delivery_order=picking,
        )
        expected_qty = formatLang(self.env, 4.0, dp="Product Unit")

        self.assertEqual(rma_line.return_scope, "components")
        self.assertIn(expected_qty, rma_line.return_component_summary)
        self.assertIn(self.product_b.display_name, rma_line.return_component_summary)

    def test_cpq_line_specific_bom_takes_priority_when_available(self):
        if "cpq_bom_id" not in self.env["sale.order.line"]._fields:
            self.skipTest("klouded_cpq is not installed")

        sale_line = self.sale_order.order_line
        self._create_bom(
            sale_line.product_id,
            self.product_b,
            1.0,
            sequence=1,
        )
        cpq_bom = self._create_bom(
            sale_line.product_id,
            self.product_b,
            3.0,
            sequence=100,
        )
        sale_line.cpq_bom_id = cpq_bom

        rma_line = self._make_sale_return_line(sale_line)
        expected_qty = formatLang(self.env, 6.0, dp="Product Unit")

        self.assertEqual(rma_line._get_return_bom(), cpq_bom)
        self.assertIn(expected_qty, rma_line.return_component_summary)

    def test_cpq_resolved_sku_delivery_is_identified_as_finished_good(self):
        if "cpq_custom_finished_product_id" not in self.env["sale.order.line"]._fields:
            self.skipTest("klouded_cpq is not installed")

        sale_line = self.sale_order.order_line
        finished_product = self.env["product.product"].create({
            "name": "Resolved Custom Finished SKU",
            "type": "consu",
        })
        finished_bom = self._create_bom(
            finished_product,
            self.product_b,
            2.0,
        )
        sale_line.sudo().write({
            "cpq_custom_finished_product_id": finished_product.id,
            "cpq_custom_finished_bom_id": finished_bom.id,
        })
        warehouse = self.sale_order.warehouse_id
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": warehouse.out_type_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
        })
        self.env["stock.move"].create({
            "product_id": finished_product.id,
            "product_uom_qty": 5.0,
            "product_uom": finished_product.uom_id.id,
            "location_id": picking.location_id.id,
            "location_dest_id": picking.location_dest_id.id,
            "picking_id": picking.id,
            "sale_line_id": sale_line.id,
        })

        rma_line = self._make_sale_return_line(
            sale_line,
            delivery_order=picking,
        )

        self.assertEqual(rma_line.return_scope, "finished_good")
        self.assertIn(self.product_b.display_name, rma_line.return_component_summary)

    def test_rma_report_renders_full_description_and_return_structure(self):
        sale_line = self.sale_order.order_line
        sale_line.name = "Configure Door\nRockport, black, double door"
        self._create_bom(
            sale_line.product_id,
            self.product_b,
            2.0,
        )
        rma_line = self._make_sale_return_line(sale_line)

        html, _content_type = self.env["ir.actions.report"]._render_qweb_html(
            "unico_rma.rma_report",
            [rma_line.rma_id.id],
        )
        rendered = html.decode()

        self.assertIn("Rockport, black, double door", rendered)
        self.assertIn("Return as:", rendered)
        self.assertIn("Finished Good", rendered)
        self.assertIn("Finished good includes:", rendered)
        self.assertIn(self.product_b.name, rendered)


@tagged("post_install", "-at_install")
class TestStockQuantSerialConstraint(RmaTestCommon):

    def test_core_serial_quantity_check_still_raises_for_duplicate_quantity(self):
        serial_product = self.env["product.product"].create({
            "name": "RMA Serial Product",
            "is_storable": True,
            "tracking": "serial",
        })
        lot = self.env["stock.lot"].create({
            "name": "RMA-SN-001",
            "product_id": serial_product.id,
        })
        quant = self.env["stock.quant"].create({
            "product_id": serial_product.id,
            "location_id": self.env.ref("stock.stock_location_stock").id,
            "lot_id": lot.id,
            "quantity": 2.0,
        })

        with self.assertRaises(ValidationError):
            quant.check_quantity()


@tagged("post_install", "-at_install")
class TestRmaResolution(RmaTestCommon):

    def test_count_rma_main_ids_is_zero_when_unused(self):
        resolution = self.env["rma.resolution"].create({
            "rma_resolution": "Unused Resolution",
            "rma_action": "refund",
        })
        self.assertEqual(resolution.count_rma, 0)

    def test_count_rma_main_ids_increments_when_referenced(self):
        resolution = self.env["rma.resolution"].create({
            "rma_resolution": "Referenced Resolution",
            "rma_action": "refund",
        })
        self._make_rma(rma_resolution_id=resolution.id)
        # Re-read to refresh non-stored compute
        resolution.invalidate_recordset(["count_rma"])
        self.assertEqual(resolution.count_rma, 1)

    def test_unlink_raises_when_resolution_used_by_rma(self):
        resolution = self.env["rma.resolution"].create({
            "rma_resolution": "In-use Resolution",
            "rma_action": "refund",
        })
        self._make_rma(rma_resolution_id=resolution.id)
        with self.assertRaises(ValidationError):
            resolution.unlink()

    def test_unlink_succeeds_when_resolution_unused(self):
        resolution = self.env["rma.resolution"].create({
            "rma_resolution": "Disposable Resolution",
            "rma_action": "refund",
        })
        resolution.unlink()


@tagged("post_install", "-at_install")
class TestRmaReplaceOrder(RmaTestCommon):

    def test_total_price_uses_list_price(self):
        rma = self._make_rma(replacements=[
            {"product_id": self.product_a.id, "qty": 3},
        ])
        replacement = rma.replace_prd_ids
        # product_a list_price = 100, qty = 3
        self.assertFalse(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 100.0)
        self.assertEqual(replacement.total_price, 300.0)

    def test_replacement_qty_preserves_fractional_quantities(self):
        rma = self._make_rma(replacements=[
            {
                "product_id": self.product_a.id,
                "qty": 42.5,
                "price_unit_manual": True,
                "manual_price_unit": 1.0,
            },
        ])
        replacement = rma.replace_prd_ids
        self.assertTrue(replacement.price_unit_manual)
        self.assertEqual(replacement.qty, 42.5)
        self.assertEqual(replacement.total_price, 42.5)

    def test_total_price_uses_manual_unit_price(self):
        rma = self._make_rma(replacements=[
            {
                "product_id": self.product_a.id,
                "qty": 2.5,
                "price_unit_manual": True,
                "manual_price_unit": 12.25,
            },
        ])
        replacement = rma.replace_prd_ids
        self.assertTrue(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 12.25)
        self.assertEqual(replacement.total_price, 30.625)

    def test_default_price_uses_variant_sales_price(self):
        attribute = self.env["product.attribute"].create({"name": "RMA Finish"})
        value = self.env["product.attribute.value"].create({
            "name": "Premium",
            "attribute_id": attribute.id,
        })
        template = self.env["product.template"].create({
            "name": "RMA Variant Replacement",
            "type": "consu",
            "list_price": 10.0,
            "attribute_line_ids": [(0, 0, {
                "attribute_id": attribute.id,
                "value_ids": [(6, 0, [value.id])],
            })],
        })
        template.attribute_line_ids.product_template_value_ids.price_extra = 7.0
        product = template.product_variant_ids

        replacement = self.env["rma.replace.order"].create({
            "product_id": product.id,
            "qty": 2.0,
        })

        self.assertFalse(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 17.0)
        self.assertEqual(replacement.total_price, 34.0)

    def test_write_product_recomputes_non_manual_price(self):
        replacement = self.env["rma.replace.order"].create({
            "product_id": self.product_a.id,
            "qty": 1.0,
        })

        replacement.write({"product_id": self.product_b.id})

        self.assertFalse(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 50.0)
        self.assertEqual(replacement.total_price, 50.0)

    def test_write_product_resets_old_manual_price_without_new_price(self):
        replacement = self.env["rma.replace.order"].create({
            "product_id": self.product_a.id,
            "qty": 1.0,
            "price_unit_manual": True,
            "manual_price_unit": 12.25,
        })

        replacement.write({"product_id": self.product_b.id})

        self.assertFalse(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 50.0)

    def test_serial_replacement_qty_must_be_whole_number(self):
        serial_product = self.env["product.product"].create({
            "name": "RMA Serial Replacement",
            "type": "consu",
            "is_storable": True,
            "tracking": "serial",
        })

        with self.assertRaises(ValidationError):
            self.env["rma.replace.order"].create({
                "product_id": serial_product.id,
                "qty": 1.5,
            })
