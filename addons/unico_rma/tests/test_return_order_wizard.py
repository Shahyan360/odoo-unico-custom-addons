from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import formatLang

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestReturnOrderWizard(RmaTestCommon):

    def _make_wizard(self, rma, rol_vals=None, replace_vals=None):
        """Build a return.order wizard pre-populated with RMA lines."""
        rma_line_ids = rma.rma_line_ids.ids
        replace_prd_ids = rma.replace_prd_ids.ids
        wizard = (
            self.env["return.order"]
            .with_context(
                default_rma_id=rma.id,
                default_rma_type=rma.rma_type or False,
                default_rma_line=rma_line_ids,
                default_replace_prd_ids=replace_prd_ids,
            )
            .create({})
        )

        if rol_vals is not None:
            for line, override in zip(wizard.rol_ids, rol_vals):
                line.write(override)
        if replace_vals is not None:
            wizard.replace_prd_ids.unlink()
            for vals in replace_vals:
                vals = dict(vals, return_id=wizard.id) if False else vals
                self.env["return.replace.order"].create(dict(vals, rma_id=wizard.id))

        return wizard

    def test_check_basic_validation_missing_return_qty(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 0,
            "return_reason_id": self.return_reason.id,
        }])
        with self.assertRaises(ValidationError):
            wizard.check_basic_validation()

    def test_check_basic_validation_missing_resolution(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": False,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        with self.assertRaises(ValidationError):
            wizard.check_basic_validation()

    def test_check_basic_validation_missing_return_reason(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 2,
            "return_reason_id": False,
        }])
        with self.assertRaises(ValidationError):
            wizard.check_basic_validation()

    def test_check_basic_validation_passes_when_complete(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        # Should not raise
        wizard.check_basic_validation()

    def test_validate_replacement_products_raises_when_replacement_action_has_no_products(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_replacement.id,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        # No replace_prd_ids on wizard
        self.assertFalse(wizard.replace_prd_ids)
        with self.assertRaises(ValidationError):
            wizard.validate_replacement_products()

    def test_validate_replacement_products_passes_when_refund_only(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        # Should not raise — refund action doesn't require replacements
        wizard.validate_replacement_products()

    def test_show_replacement_tab_true_for_replacement_action(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_replacement.id,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        wizard.invalidate_recordset(["show_replacement_tab"])
        self.assertTrue(wizard.show_replacement_tab)

    def test_show_replacement_tab_false_for_refund_action(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 2,
            "return_reason_id": self.return_reason.id,
        }])
        wizard.invalidate_recordset(["show_replacement_tab"])
        self.assertFalse(wizard.show_replacement_tab)

    def test_default_get_populates_rol_ids_from_rma(self):
        rma = self._make_rma(rma_type="rma_with_do", lines=[
            {"product_id": self.product_a.id, "return_qty": 0, "price_unit": 100.0, "delivery_qty": 5.0},
            {"product_id": self.product_b.id, "return_qty": 0, "price_unit": 50.0, "delivery_qty": 5.0},
        ])
        wizard = (
            self.env["return.order"]
            .with_context(
                default_rma_id=rma.id,
                default_rma_type=rma.rma_type,
                default_rma_line=rma.rma_line_ids.ids,
            )
            .create({})
        )
        self.assertEqual(len(wizard.rol_ids), 2)
        self.assertEqual(wizard.rma_order_id, rma)
        self.assertEqual(wizard.rma_type, "rma_with_do")

    def test_component_summary_tracks_wizard_return_quantity(self):
        sale_line = self.sale_order.order_line
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.product_a.product_tmpl_id.id,
            "product_id": self.product_a.id,
            "product_qty": 1.0,
            "product_uom_id": self.product_a.uom_id.id,
            "type": "normal",
            "bom_line_ids": [(0, 0, {
                "product_id": self.product_b.id,
                "product_qty": 2.0,
                "product_uom_id": self.product_b.uom_id.id,
            })],
        })
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0.0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "sale_line_id": sale_line.id,
        }])
        wizard = self._make_wizard(rma)
        wizard_line = wizard.rol_ids

        full_qty = formatLang(self.env, 10.0, dp="Product Unit")
        partial_qty = formatLang(self.env, 4.0, dp="Product Unit")
        self.assertIn(full_qty, wizard_line.return_component_summary)

        wizard_line.return_qty = 2.0
        self.assertIn(partial_qty, wizard_line.return_component_summary)

    def test_update_rma_lines_blocks_cumulative_over_return_for_sale_line(self):
        sale_line = self.sale_order.order_line
        sale_line.qty_delivered = 5.0
        self._make_rma(
            rma_type="rma_with_do",
            lines=[{
                "product_id": self.product_a.id,
                "return_qty": 3,
                "price_unit": 100.0,
                "delivery_qty": 5.0,
                "sale_line_id": sale_line.id,
                "rma_resolution_id": self.resolution_refund.id,
                "return_reason_id": self.return_reason.id,
            }],
            state="submit",
        )
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "sale_line_id": sale_line.id,
        }])
        wizard = self._make_wizard(rma, rol_vals=[{
            "rma_resolution_id": self.resolution_refund.id,
            "return_qty": 3,
            "return_reason_id": self.return_reason.id,
            "is_new_line": True,
        }])

        with self.assertRaises(ValidationError):
            wizard.update_rma_lines()

    def test_update_rma_lines_preserves_fractional_replacement_qty_and_price(self):
        sale_line = self.sale_order.order_line
        sale_line.qty_delivered = 5.0
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "sale_line_id": sale_line.id,
        }])
        wizard = self._make_wizard(
            rma,
            rol_vals=[{
                "rma_resolution_id": self.resolution_replacement.id,
                "return_qty": 1.0,
                "return_reason_id": self.return_reason.id,
                "is_new_line": True,
            }],
            replace_vals=[{
                "product_id": self.product_b.id,
                "qty": 42.5,
                "price_unit_manual": True,
                "manual_price_unit": 1.0,
            }],
        )

        wizard.update_rma_lines()

        replacement = rma.replace_prd_ids
        self.assertEqual(replacement.qty, 42.5)
        self.assertTrue(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 1.0)
        self.assertEqual(replacement.total_price, 42.5)

    def test_update_rma_lines_keeps_default_replacement_price_non_manual(self):
        sale_line = self.sale_order.order_line
        sale_line.qty_delivered = 5.0
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "sale_line_id": sale_line.id,
        }])
        wizard = self._make_wizard(
            rma,
            rol_vals=[{
                "rma_resolution_id": self.resolution_replacement.id,
                "return_qty": 1.0,
                "return_reason_id": self.return_reason.id,
                "is_new_line": True,
            }],
            replace_vals=[{
                "product_id": self.product_b.id,
                "qty": 2.0,
            }],
        )

        wizard.update_rma_lines()

        replacement = rma.replace_prd_ids
        self.assertFalse(replacement.price_unit_manual)
        self.assertEqual(replacement.price_unit, 50.0)

    def test_update_rma_lines_blocks_fractional_serial_replacement_qty(self):
        sale_line = self.sale_order.order_line
        sale_line.qty_delivered = 5.0
        serial_product = self.env["product.product"].create({
            "name": "RMA Wizard Serial Replacement",
            "type": "consu",
            "is_storable": True,
            "tracking": "serial",
        })
        rma = self._make_rma(rma_type="rma_with_do", lines=[{
            "product_id": self.product_a.id,
            "return_qty": 0,
            "price_unit": 100.0,
            "delivery_qty": 5.0,
            "sale_line_id": sale_line.id,
        }])
        wizard = self._make_wizard(
            rma,
            rol_vals=[{
                "rma_resolution_id": self.resolution_replacement.id,
                "return_qty": 1.0,
                "return_reason_id": self.return_reason.id,
                "is_new_line": True,
            }],
            replace_vals=[{
                "product_id": serial_product.id,
                "qty": 1.5,
            }],
        )

        with self.assertRaises(ValidationError):
            wizard.update_rma_lines()
