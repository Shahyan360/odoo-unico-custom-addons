import json
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoDeliveryDateLeadTime(TransactionCase):
    """Door 1-day lead time, its combination with other lines, the blank-on-
    quotation / filled-on-confirmation rule, and the Delivery Date lock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({"name": "Lead Time Customer"})

        cls.door_type_attr = cls.env["product.attribute"].create({
            "name": "Door Type", "cpq_code": "door_type",
        })
        cls.single_value = cls.env["product.attribute.value"].create({
            "name": "Single Door", "cpq_code": "single_door",
            "attribute_id": cls.door_type_attr.id,
        })
        cls.double_value = cls.env["product.attribute.value"].create({
            "name": "Double Door", "cpq_code": "double_door",
            "attribute_id": cls.door_type_attr.id,
        })

        # Configurable door with a deliberately long product lead time, to prove
        # the 1-day door rule overrides the product's own sale_delay.
        cls.door_product = cls.env["product.template"].create({
            "name": "Configure Door",
            "type": "consu",
            "is_klouded_configurable": True,
            "sale_delay": 30,
        }).product_variant_id
        cls.long_product = cls.env["product.template"].create({
            "name": "Long Lead Item", "type": "consu", "sale_delay": 10,
        }).product_variant_id

    def _order_with_lines(self, *line_vals):
        order = self.env["sale.order"].create({"partner_id": self.customer.id})
        for vals in line_vals:
            self.env["sale.order.line"].create({"order_id": order.id, **vals})
        return order

    def _door_line_vals(self, value):
        return {
            "product_id": self.door_product.id,
            "product_uom_qty": 1,
            "cpq_configuration_data": json.dumps({"door_type": value.id}),
        }

    def _days_out(self, order):
        return (order.commitment_date.date() - fields.Date.context_today(order)).days

    # -- Door 1-day lead ---------------------------------------------------

    def test_single_door_defaults_to_one_day(self):
        order = self._order_with_lines(self._door_line_vals(self.single_value))
        order._apply_customer_lead_time()
        self.assertEqual(self._days_out(order), 1)

    def test_double_door_defaults_to_one_day(self):
        order = self._order_with_lines(self._door_line_vals(self.double_value))
        order._apply_customer_lead_time()
        self.assertEqual(self._days_out(order), 1)

    def test_door_line_helper_flags_door(self):
        order = self._order_with_lines(self._door_line_vals(self.single_value))
        line = order.order_line
        self.assertTrue(line._unico_is_cpq_single_or_double_door())
        self.assertEqual(line._unico_customer_lead_time(), 1)

    def test_longer_non_door_line_wins_over_door(self):
        """A door contributes 1 day, but a longer-lead item still drives the max."""
        order = self._order_with_lines(
            self._door_line_vals(self.single_value),
            {"product_id": self.long_product.id, "product_uom_qty": 1},
        )
        order._apply_customer_lead_time()
        self.assertEqual(self._days_out(order), 10)

    # -- Blank on quotation, derived on confirmation -----------------------

    def test_quotation_keeps_an_empty_delivery_date(self):
        """Adding lead-time lines to a quotation must not put a date in: the
        Delivery Date stays blank until the order is confirmed."""
        order = self._order_with_lines(
            self._door_line_vals(self.single_value),
            {"product_id": self.long_product.id, "product_uom_qty": 1},
        )
        self.assertFalse(order.commitment_date)
        self.assertFalse(order.unico_auto_commitment_date)

    def test_confirmation_fills_an_empty_delivery_date(self):
        """Uses the plain long-lead product rather than a door: confirming a
        configurable door line drives CPQ manufacturing-order creation, which
        needs BOM mappings this fixture has no reason to set up."""
        order = self._order_with_lines({
            "product_id": self.long_product.id,
            "product_uom_qty": 1,
        })
        self.assertFalse(order.commitment_date)
        order.action_confirm()
        self.assertEqual(self._days_out(order), 10)
        self.assertEqual(order.unico_auto_commitment_date, order.commitment_date)

    def test_manual_delivery_date_survives_confirmation(self):
        """A date entered by hand on the quotation is never recomputed."""
        order = self._order_with_lines({
            "product_id": self.long_product.id,
            "product_uom_qty": 1,
        })
        chosen = fields.Datetime.now() + timedelta(days=45)
        order.commitment_date = chosen
        order.action_confirm()
        self.assertEqual(order.commitment_date, chosen)
        self.assertFalse(order.unico_auto_commitment_date)

    def test_manual_override_is_not_overwritten_by_reapply(self):
        order = self._order_with_lines(self._door_line_vals(self.single_value))
        override = fields.Datetime.now() + timedelta(days=5)
        order.commitment_date = override
        order._apply_customer_lead_time()
        self.assertEqual(order.commitment_date, override)

    def test_apply_sets_date_on_order_without_one(self):
        order = self._order_with_lines(self._door_line_vals(self.single_value))
        self.assertFalse(order.commitment_date)
        order._apply_customer_lead_time()
        self.assertEqual(self._days_out(order), 1)

    # -- Delivery Date lock ------------------------------------------------

    def test_delivery_date_locked_for_non_manager(self):
        manager = self.env["res.users"].create({
            "name": "Sale Mgr", "login": "unico_sale_mgr",
            "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_manager").id])],
        })
        clerk = self.env["res.users"].create({
            "name": "Sale Clerk", "login": "unico_sale_clerk",
            "group_ids": [(6, 0, [self.env.ref("sales_team.group_sale_salesman").id])],
        })
        order = self._order_with_lines(self._door_line_vals(self.single_value))
        self.assertFalse(order.with_user(manager).unico_delivery_date_readonly)
        self.assertTrue(order.with_user(clerk).unico_delivery_date_readonly)
