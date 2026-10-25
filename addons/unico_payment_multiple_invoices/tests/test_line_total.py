"""Set Line Total on a sale order line.

Sales agrees a figure for the whole line, not a unit price. ``price_subtotal``
is computed from ``price_unit``, so the workflow inverts that computation
rather than making the subtotal writable. These tests pin the contract: the
entered total becomes the exact pre-tax charge, and Unit Price and margin are
derived from it.
"""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUnicoLineTotal(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(user=cls.env.ref("base.user_admin"))
        cls.product = cls.env["product.product"].create({
            "name": "Line Total Test Item",
            "type": "consu",
            "list_price": 100.0,
            "standard_price": 60.0,
        })
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.partner_a.id,
        })
        cls.line = cls.env["sale.order.line"].create({
            "order_id": cls.order.id,
            "product_id": cls.product.id,
            "product_uom_qty": 4.0,
            "price_unit": 100.0,
            "purchase_price": 60.0,
            "tax_ids": [Command.clear()],
        })

    def _open(self, line):
        action = line.action_open_line_total_wizard()
        # The widget fetches this over a plain ORM call, which skips the
        # controller that would otherwise derive `views` from `view_mode`.
        # Without it the web client crashes reading `action.views`.
        self.assertEqual(action["views"], [(False, "form")])
        return self.env["unico.line.total.wizard"].browse(action["res_id"])

    def test_line_total_back_solves_unit_price_and_margin(self):
        """The entered total becomes the exact pre-tax charge."""
        wizard = self._open(self.line)
        self.assertAlmostEqual(wizard.current_subtotal, 400.0, places=2)
        self.assertAlmostEqual(wizard.current_margin, 160.0, places=2)
        self.assertAlmostEqual(wizard.current_margin_percent, 0.4, places=4)

        wizard.target_subtotal = 300.0
        self.assertAlmostEqual(wizard.new_price_unit, 75.0, places=2)
        self.assertAlmostEqual(wizard.new_margin, 60.0, places=2)
        self.assertAlmostEqual(wizard.new_margin_percent, 0.2, places=4)
        self.assertFalse(wizard.rounding_notice)

        wizard.action_apply()
        self.line.invalidate_recordset()

        self.assertAlmostEqual(self.line.price_unit, 75.0, places=2)
        self.assertAlmostEqual(self.line.price_subtotal, 300.0, places=2)
        self.assertAlmostEqual(self.line.margin, 60.0, places=2)
        self.assertAlmostEqual(self.line.margin_percent, 0.2, places=4)

    def test_line_total_clears_an_existing_discount(self):
        """A negotiated total is unambiguous only with the discount cleared."""
        self.line.write({"discount": 10.0})
        self.assertAlmostEqual(self.line.price_subtotal, 360.0, places=2)

        wizard = self._open(self.line)
        wizard.target_subtotal = 300.0
        wizard.action_apply()
        self.line.invalidate_recordset()

        self.assertAlmostEqual(self.line.discount, 0.0, places=2)
        self.assertAlmostEqual(self.line.price_unit, 75.0, places=2)
        self.assertAlmostEqual(self.line.price_subtotal, 300.0, places=2)

    def test_line_total_inverts_a_price_included_tax(self):
        """Dividing the total by quantity is wrong once taxes are included."""
        tax = self.env["account.tax"].create({
            "name": "Included 10%",
            "amount_type": "percent",
            "amount": 10.0,
            "price_include_override": "tax_included",
            "type_tax_use": "sale",
        })
        self.line.write({"tax_ids": [Command.set(tax.ids)]})

        wizard = self._open(self.line)
        wizard.target_subtotal = 300.0
        # 300 excluding tax over 4 units needs a tax-inclusive unit price of 82.50.
        self.assertAlmostEqual(wizard.new_price_unit, 82.5, places=2)

        wizard.action_apply()
        self.line.invalidate_recordset()
        self.assertAlmostEqual(self.line.price_subtotal, 300.0, places=2)

    def test_line_total_reports_unreachable_rounding(self):
        """Say which total will really be charged instead of silently differing."""
        self.line.write({"product_uom_qty": 3.0})
        wizard = self._open(self.line)
        wizard.target_subtotal = 100.0

        self.assertTrue(wizard.rounding_notice)
        self.assertAlmostEqual(wizard.new_price_unit, 33.33, places=2)
        self.assertAlmostEqual(wizard.new_subtotal, 99.99, places=2)

        wizard.action_apply()
        self.line.invalidate_recordset()
        self.assertAlmostEqual(self.line.price_subtotal, 99.99, places=2)

    def test_line_total_is_unavailable_on_sections(self):
        section = self.env["sale.order.line"].create({
            "order_id": self.order.id,
            "display_type": "line_section",
            "name": "Doors",
        })
        self.assertFalse(section.unico_can_set_line_total)

    def test_line_total_is_unavailable_without_a_quantity(self):
        self.line.write({"product_uom_qty": 0.0})
        self.line.invalidate_recordset()

        self.assertFalse(self.line.unico_can_set_line_total)
        with self.assertRaisesRegex(UserError, "can no longer be changed"):
            self.line.action_open_line_total_wizard()

    def test_line_total_is_unavailable_once_the_order_is_confirmed(self):
        self.order.action_confirm()
        self.line.invalidate_recordset()

        self.assertFalse(self.line.unico_can_set_line_total)
        with self.assertRaisesRegex(UserError, "can no longer be changed"):
            self.line.action_open_line_total_wizard()
