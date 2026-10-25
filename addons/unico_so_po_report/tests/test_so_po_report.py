# -*- coding: utf-8 -*-
import base64
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSoPoReport(TransactionCase):
    """Covers the criteria (period, status, coverage, RFQ toggle), the sale <->
    purchase roll-up, and the workbook the wizard hands back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaleOrder = cls.env["sale.order"]
        cls.PurchaseOrder = cls.env["purchase.order"]
        cls.Wizard = cls.env["unico.so.po.report.wizard"]

        cls.customer = cls.env["res.partner"].create({"name": "Report Customer"})
        cls.vendor = cls.env["res.partner"].create({"name": "Report Vendor"})
        cls.other_vendor = cls.env["res.partner"].create({"name": "Other Vendor"})
        tmpl = cls.env["product.template"].create({
            "name": "Report Test Door",
            "type": "consu",
            "is_storable": True,
            "list_price": 500.0,
            "standard_price": 300.0,
        })
        cls.product = tmpl.product_variant_id
        cls.today = fields.Date.context_today(cls.env.user)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    # The test product is stockable with no stock on hand, so unico_delivery's
    # _check_unico_purchase_note_for_shortfall requires a sourcing note on every
    # line before the order will save. Same convention as unico_wip_report.
    PURCHASE_NOTE = "Source from vendor"

    def _order_line(self, qty=2):
        return (0, 0, {
            "product_id": self.product.id,
            "product_uom_qty": qty,
            "unico_purchase_note": self.PURCHASE_NOTE,
        })

    def _make_order(self, confirm=True, qty=2):
        order = self.SaleOrder.create({
            "partner_id": self.customer.id,
            "order_line": [self._order_line(qty)],
        })
        if confirm:
            order.action_confirm()
        return order

    def _make_purchase(self, order, vendor=None, state=None, price=300.0, qty=2):
        """A purchase linked the way the real flows link: origin names the sale."""
        purchase = self.PurchaseOrder.create({
            "partner_id": (vendor or self.vendor).id,
            "origin": order.name,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": qty,
                "price_unit": price,
            })],
        })
        if state == "purchase":
            purchase.button_confirm()
        elif state == "cancel":
            purchase.button_cancel()
        return purchase

    def _wizard(self, **values):
        vals = {
            "date_from": self.today - timedelta(days=7),
            "date_to": self.today + timedelta(days=1),
        }
        vals.update(values)
        return self.Wizard.create(vals)

    def _row_for(self, wizard, order):
        return next(
            (row for row in wizard._report_rows() if row["order"] == order), None)

    # ------------------------------------------------------------------
    # Matching and the money
    # ------------------------------------------------------------------
    def test_origin_linked_purchase_is_reported(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        row = self._row_for(self._wizard(), order)
        self.assertTrue(row, "An order with an origin-linked PO must be reported")
        self.assertEqual(row["so_untaxed"], order.amount_untaxed)
        self.assertEqual(row["po_untaxed"], 600.0)
        self.assertEqual(row["revenue"], order.amount_untaxed - 600.0)
        self.assertEqual(row["vendors"], [self.vendor.name])

    def test_purchase_numbers_are_listed_per_order(self):
        order = self._make_order()
        first = self._make_purchase(order, state="purchase")
        second = self._make_purchase(order, vendor=self.other_vendor,
                                     state="purchase")
        row = self._row_for(self._wizard(), order)
        self.assertEqual(sorted(row["po_names"]), sorted([first.name, second.name]))

    def test_vendors_are_listed_once_each(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        self._make_purchase(order, state="purchase")
        self._make_purchase(order, vendor=self.other_vendor, state="purchase")
        row = self._row_for(self._wizard(), order)
        self.assertEqual(len(row["purchases"]), 3)
        self.assertEqual(sorted(row["vendors"]),
                         sorted([self.vendor.name, self.other_vendor.name]))

    def test_revenue_percent_of_the_sale(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase", price=100.0)
        row = self._row_for(self._wizard(), order)
        self.assertAlmostEqual(
            row["revenue_percent"], row["revenue"] / row["so_untaxed"], places=6)

    def test_cancelled_purchases_do_not_count(self):
        order = self._make_order()
        self._make_purchase(order, state="cancel")
        self.assertIsNone(
            self._row_for(self._wizard(), order),
            "A cancelled PO is not committed spend, so the order has no purchases")

    def test_rfq_toggle(self):
        order = self._make_order()
        self._make_purchase(order)  # left as a draft RFQ
        self.assertTrue(self._row_for(self._wizard(include_rfq=True), order))
        self.assertIsNone(self._row_for(self._wizard(include_rfq=False), order))

    # ------------------------------------------------------------------
    # Criteria
    # ------------------------------------------------------------------
    def test_orders_without_purchases_follow_the_coverage_setting(self):
        order = self._make_order()
        self.assertIsNone(self._row_for(self._wizard(), order))
        row = self._row_for(self._wizard(purchase_scope="all"), order)
        self.assertTrue(row, "'All orders' must report a sale sourced from stock")
        self.assertEqual(row["po_untaxed"], 0.0)
        self.assertEqual(row["revenue"], row["so_untaxed"])

    def test_orders_outside_the_period_are_dropped(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        order.date_order = fields.Datetime.now() - timedelta(days=60)
        self.assertIsNone(self._row_for(self._wizard(), order))
        old = self._wizard(date_from=self.today - timedelta(days=90))
        self.assertTrue(self._row_for(old, order))

    def test_status_filter(self):
        quotation = self._make_order(confirm=False)
        self._make_purchase(quotation, state="purchase")
        confirmed = self._make_order()
        self._make_purchase(confirmed, state="purchase")

        open_orders = self._wizard(state_filter="open")
        self.assertTrue(self._row_for(open_orders, confirmed))
        self.assertIsNone(self._row_for(open_orders, quotation))

        quotes = self._wizard(state_filter="quotation")
        self.assertTrue(self._row_for(quotes, quotation))
        self.assertIsNone(self._row_for(quotes, confirmed))

        everything = self._wizard(state_filter="not_cancelled")
        self.assertTrue(self._row_for(everything, quotation))
        self.assertTrue(self._row_for(everything, confirmed))

    def test_period_must_run_forwards(self):
        with self.assertRaises(ValidationError):
            self._wizard(date_from=self.today, date_to=self.today - timedelta(days=1))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def test_preview_totals_match_the_rows(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        wizard = self._wizard()
        rows = wizard._report_rows()
        self.assertEqual(wizard.order_count, len(rows))
        self.assertEqual(wizard.purchase_count,
                         sum(len(row["purchases"]) for row in rows))
        self.assertAlmostEqual(wizard.revenue_total,
                               sum(row["revenue"] for row in rows), places=2)
        self.assertEqual(wizard.currency_id, wizard.company_id.currency_id)

    def test_export_returns_a_downloadable_workbook(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        wizard = self._wizard()
        action = wizard.action_export_xlsx()
        self.assertEqual(action["type"], "ir.actions.act_url")
        attachment = self.env["ir.attachment"].search(
            [("res_model", "=", wizard._name), ("res_id", "=", wizard.id)], limit=1)
        self.assertTrue(attachment)
        self.assertIn(str(attachment.id), action["url"])
        self.assertTrue(attachment.name.endswith(".xlsx"))
        # A real xlsx is a zip container.
        self.assertTrue(base64.b64decode(attachment.datas).startswith(b"PK"))

    def test_export_without_matches_explains_itself(self):
        wizard = self._wizard(date_from=self.today - timedelta(days=3650),
                              date_to=self.today - timedelta(days=3600))
        with self.assertRaises(UserError):
            wizard.action_export_xlsx()

    def test_view_orders_opens_the_same_selection(self):
        order = self._make_order()
        self._make_purchase(order, state="purchase")
        action = self._wizard().action_view_orders()
        self.assertEqual(action["res_model"], "sale.order")
        self.assertIn(order.id, action["domain"][0][2])
