from ast import literal_eval
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged("post_install", "-at_install")
class TestUnicoPurchase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        cls.vendor = cls.env["res.partner"].create({"name": "Test Vendor"})
        cls.customer = cls.env["res.partner"].create({"name": "Test Customer"})
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.stock_loc = cls.warehouse.lot_stock_id
        buy_route = cls.env["stock.route"].search([("name", "ilike", "Buy")], limit=1)

        cls.product = cls.env["product.product"].create({
            "name": "Reorder Widget",
            "type": "consu",
            "is_storable": True,
            "list_price": 50.0,
            "standard_price": 30.0,
            "route_ids": [(6, 0, buy_route.ids)] if buy_route else False,
            "seller_ids": [(0, 0, {"partner_id": cls.vendor.id, "price": 30.0})],
        })

    def _set_on_hand(self, qty):
        self.env["stock.quant"]._update_available_quantity(
            self.product, self.stock_loc, qty
        )
        self.product.invalidate_recordset()

    def _line_vals(self, product, qty):
        """Order-line vals. When unico_delivery is also installed it blocks
        saving a short stockable line without a Purchase Note, so we supply one
        when that field exists (and harmlessly omit it otherwise)."""
        vals = {"product_id": product.id, "product_uom_qty": qty}
        if "unico_purchase_note" in self.env["sale.order.line"]._fields:
            vals["unico_purchase_note"] = "Auto test sourcing note"
        return vals

    def _make_orderpoint(self, trigger="auto", min_qty=10.0, max_qty=20.0):
        return self.env["stock.warehouse.orderpoint"].create({
            "product_id": self.product.id,
            "warehouse_id": self.warehouse.id,
            "location_id": self.stock_loc.id,
            "product_min_qty": min_qty,
            "product_max_qty": max_qty,
            "trigger": trigger,
        })

    def _confirm_sale(self, qty=8.0, commitment_days=1):
        so = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "commitment_date": fields.Datetime.now() + timedelta(days=commitment_days),
            "order_line": [(0, 0, self._line_vals(self.product, qty))],
        })
        so.action_confirm()
        return so

    def test_01_reorder_triggered_on_confirm_auto(self):
        """Auto orderpoint -> confirming a sale immediately creates an RFQ."""
        self._make_orderpoint(trigger="auto")
        self._set_on_hand(5.0)
        before = self.env["purchase.order"].search_count([("partner_id", "=", self.vendor.id)])
        self._confirm_sale(qty=8.0)
        after = self.env["purchase.order"].search([
            ("partner_id", "=", self.vendor.id),
            ("order_line.product_id", "=", self.product.id),
        ])
        self.assertTrue(after, "Confirming the sale should have generated an RFQ")
        self.assertEqual(before, 0)

    def test_01b_no_reorder_when_in_stock(self):
        """Confirming a sale must NOT create an RFQ while stock stays above min."""
        self._make_orderpoint(trigger="auto", min_qty=10.0, max_qty=20.0)
        self._set_on_hand(50.0)            # well above min, even after the sale
        self._confirm_sale(qty=5.0)        # forecast 45 -> still above max
        rfqs = self.env["purchase.order"].search([
            ("partner_id", "=", self.vendor.id),
            ("order_line.product_id", "=", self.product.id),
        ])
        self.assertFalse(
            rfqs, "No RFQ should be created when the product is in stock above min")

    def test_02_reorder_not_triggered_for_manual(self):
        """Manual orderpoint -> confirming a sale does NOT auto-create an RFQ."""
        self._make_orderpoint(trigger="manual")
        self._set_on_hand(5.0)
        self._confirm_sale(qty=8.0)
        rfqs = self.env["purchase.order"].search([
            ("partner_id", "=", self.vendor.id),
            ("order_line.product_id", "=", self.product.id),
        ])
        self.assertFalse(rfqs, "Manual rules must not auto-generate an RFQ on confirm")

    def test_03_sale_purchase_smart_button(self):
        """SO surfaces the purchases auto-created *for it* (matched by origin),
        not every open PO that happens to share a product."""
        v1 = self.env["res.partner"].create({"name": "Smart Button Vendor"})
        product = self._special_product("Smart Button Special", sellers=[(v1, 6.0)])
        so = self._confirm_special_sale(product, qty=4.0)
        so.invalidate_recordset()
        self.assertGreaterEqual(so.unico_purchase_count, 1)
        action = so.action_unico_view_purchases()
        self.assertEqual(action["res_model"], "purchase.order")
        # An unrelated open PO containing the same product must NOT be counted.
        other_po = self.env["purchase.order"].create({
            "partner_id": v1.id,
            "origin": "Unrelated",
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": product.display_name,
                "product_qty": 1.0,
                "product_uom_id": product.uom_id.id,
                "price_unit": 6.0,
            })],
        })
        so.invalidate_recordset()
        self.assertNotIn(other_po, so._unico_related_purchase_orders())

    def test_04_purchase_urgency_from_commitment(self):
        """PO urgency buckets from the earliest linked sale's commitment date."""
        self._make_orderpoint(trigger="auto")
        self._set_on_hand(5.0)
        self._confirm_sale(qty=8.0, commitment_days=1)  # within 3 days -> urgent
        po = self.env["purchase.order"].search([
            ("order_line.product_id", "=", self.product.id),
        ], limit=1)
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "urgent")
        self.assertTrue(po.unico_earliest_commitment)

    def test_05_dashboard_payload_has_urgency(self):
        """retrieve_dashboard is extended with an urgency breakdown."""
        result = self.env["purchase.order"].retrieve_dashboard()
        self.assertIn("urgency", result)
        self.assertEqual(
            set(result["urgency"].keys()), {"overdue", "urgent", "normal"}
        )

    def test_06_changing_commitment_after_confirm_updates_urgency(self):
        """Editing the commitment date on a confirmed sale re-buckets the PO."""
        self._make_orderpoint(trigger="auto")
        self._set_on_hand(5.0)
        so = self._confirm_sale(qty=8.0, commitment_days=30)  # far -> normal
        po = self.env["purchase.order"].search(
            [("order_line.product_id", "=", self.product.id)], limit=1)
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "normal")
        # Move the promise closer -> must become urgent without any PO change.
        so.commitment_date = fields.Datetime.now() + timedelta(days=1)
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "urgent")
        # Push it into the past -> overdue.
        so.commitment_date = fields.Datetime.now() - timedelta(days=2)
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "overdue")

    def test_07_draft_quotation_drives_urgency_before_confirm(self):
        """An unconfirmed quotation's commitment date already affects urgency."""
        self._make_orderpoint(trigger="auto")
        self._set_on_hand(5.0)
        # A confirmed sale far in the future creates the PO (normal).
        self._confirm_sale(qty=8.0, commitment_days=60)
        po = self.env["purchase.order"].search(
            [("order_line.product_id", "=", self.product.id)], limit=1)
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "normal")
        # A brand-new *draft* quotation due tomorrow must pull urgency to urgent.
        self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "commitment_date": fields.Datetime.now() + timedelta(days=1),
            "order_line": [(0, 0, self._line_vals(self.product, 2.0))],
        })
        po.invalidate_recordset()
        self.assertEqual(po.unico_urgency, "urgent")

    def test_07b_salesperson_without_purchase_access_can_create_urgent_quote(self):
        """Sales quote creation must not require Purchase app access just because
        the purchase dashboard keeps product-linked urgency buckets fresh."""
        po = self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "name": self.product.display_name,
                "product_qty": 1.0,
                "product_uom_id": self.product.uom_id.id,
                "price_unit": 30.0,
            })],
        })
        quote_user = new_test_user(
            self.env,
            login="sales_quote_no_purchase",
            groups="base.group_user,sales_team.group_sale_salesman",
            company_id=self.company.id,
            company_ids=[(6, 0, self.company.ids)],
        )
        self.assertFalse(quote_user.has_group("purchase.group_purchase_user"))
        with self.assertRaises(AccessError):
            po.with_user(quote_user).read(["name"])

        so = self.env["sale.order"].with_user(quote_user).create({
            "partner_id": self.customer.id,
            "commitment_date": fields.Datetime.now() + timedelta(days=1),
            "order_line": [(0, 0, self._line_vals(self.product, 2.0))],
        })

        self.assertEqual(so.user_id, quote_user)
        po.invalidate_recordset(["unico_earliest_commitment", "unico_urgency"])
        self.assertEqual(po.unico_urgency, "urgent")

    # ---- special-order auto-purchasing (products with no Min/Max rule) ----
    def _special_product(self, name, sellers=()):
        tmpl = self.env["product.template"].create({
            "name": name,
            "type": "consu",                 # non-stock special-order good
            "standard_price": 12.0,
            "seller_ids": [
                (0, 0, {"partner_id": v.id, "price": price, "sequence": seq})
                for seq, (v, price) in enumerate(sellers, start=1)
            ],
        })
        return tmpl.product_variant_id

    def _confirm_special_sale(self, product, qty=4.0):
        so = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [(0, 0, self._line_vals(product, qty))],
        })
        so.action_confirm()
        return so

    def test_08_special_order_uses_first_vendor(self):
        """No rule + not in stock -> RFQ for the shortfall with the 1st vendor."""
        v1 = self.env["res.partner"].create({"name": "Vendor One"})
        v2 = self.env["res.partner"].create({"name": "Vendor Two"})
        product = self._special_product("Special A", sellers=[(v1, 7.0), (v2, 9.0)])
        so = self._confirm_special_sale(product, qty=4.0)
        pos = self.env["purchase.order"].search([("origin", "=", so.name)])
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos.partner_id, v1, "Must use the first vendor on the product")
        pol = pos.order_line.filtered(lambda l: l.product_id == product)
        self.assertEqual(pol.product_qty, 4.0, "RFQ qty must equal the shortfall")

    def test_09_special_order_falls_back_to_default_vendor(self):
        """Product with no vendor -> uses the Purchase-settings default vendor."""
        default_vendor = self.env["res.partner"].create({"name": "Default Vendor"})
        self.company.unico_default_purchase_vendor_id = default_vendor
        product = self._special_product("Special B")  # no vendor on product
        so = self._confirm_special_sale(product, qty=3.0)
        pos = self.env["purchase.order"].search([("origin", "=", so.name)])
        self.assertEqual(pos.partner_id, default_vendor)

    def test_10_special_order_no_vendor_alerts_and_skips(self):
        """No vendor and no default -> no RFQ, purchasing is alerted on the SO."""
        self.company.unico_default_purchase_vendor_id = False
        product = self._special_product("Special C")
        so = self._confirm_special_sale(product, qty=2.0)
        self.assertFalse(self.env["purchase.order"].search([("origin", "=", so.name)]))
        self.assertTrue(
            any("needs a vendor" in (m.body or "") for m in so.message_ids),
            "Purchasing should be alerted when a special item has no vendor")

    def test_11_minmax_product_makes_no_special_rfq(self):
        """A product WITH a Min/Max rule is left to native replenishment."""
        self._make_orderpoint(trigger="auto")
        self._set_on_hand(5.0)
        so = self._confirm_sale(qty=8.0)
        # No special-order RFQ tagged with the sale name was created by us.
        self.assertFalse(
            self.env["purchase.order"].search([("origin", "=", so.name)]),
            "Min/Max products must not get a special-order RFQ")

    def test_12_special_order_skipped_when_in_stock(self):
        """In-stock special-order product -> no RFQ (no shortfall)."""
        v1 = self.env["res.partner"].create({"name": "Vendor One"})
        product = self._special_product("Special D", sellers=[(v1, 5.0)])
        product.write({"is_storable": True})
        self.env["stock.quant"]._update_available_quantity(
            product, self.stock_loc, 50.0)
        product.invalidate_recordset()
        so = self._confirm_special_sale(product, qty=4.0)
        self.assertFalse(self.env["purchase.order"].search([("origin", "=", so.name)]))

    def test_13_rfq_failure_does_not_block_confirmation(self):
        """If RFQ creation raises, the sale still confirms and purchasing is told."""
        v1 = self.env["res.partner"].create({"name": "Vendor One"})
        product = self._special_product("Special E", sellers=[(v1, 5.0)])
        so = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [(0, 0, self._line_vals(product, 4.0))],
        })
        po_model = type(self.env["purchase.order"])
        with patch.object(po_model, "create", side_effect=ValidationError("boom")):
            so.action_confirm()  # must not raise
        self.assertEqual(so.state, "sale", "Sale must confirm despite RFQ failure")
        self.assertFalse(self.env["purchase.order"].search([("origin", "=", so.name)]))
        self.assertTrue(
            any("Could not auto-create" in (m.body or "") for m in so.message_ids),
            "Purchasing should be alerted that the RFQ could not be created")

    def _make_merge_rfq(
        self, product, *, date_order, date_planned, line_name, price, qty=1.0,
        group_key=False,
    ):
        return self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "date_order": date_order,
            "unico_po_group_key": group_key,
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": line_name,
                "product_qty": qty,
                "product_uom_id": product.uom_id.id,
                "date_planned": date_planned,
                "price_unit": price,
            })],
        })

    def _merge_rfq_lines(self, first_line_vals, second_line_vals):
        product = self.env["product.product"].create({
            "name": "Merge Safety Product",
            "type": "consu",
            "is_storable": True,
            "purchase_ok": True,
        })
        planned = fields.Datetime.now() + timedelta(days=5)
        older = self._make_merge_rfq(
            product,
            date_order=fields.Datetime.now() - timedelta(days=2),
            date_planned=planned,
            **first_line_vals,
        )
        newer = self._make_merge_rfq(
            product,
            date_order=fields.Datetime.now() - timedelta(days=1),
            date_planned=planned,
            **second_line_vals,
        )

        (older | newer).action_unico_merge()
        return self.env["purchase.order.line"].search(
            [("order_id", "=", older.id), ("display_type", "=", False)],
            order="id",
        )

    def test_14_rfq_merge_keeps_distinct_descriptions(self):
        """Same product lines with different buying descriptions must not collapse."""
        lines = self._merge_rfq_lines(
            {"line_name": "Custom door frame - left hand", "price": 25.0, "qty": 1.0},
            {"line_name": "Custom door frame - right hand", "price": 25.0, "qty": 2.0},
        )

        self.assertEqual(len(lines), 2)
        self.assertEqual(set(lines.mapped("name")), {
            "Custom door frame - left hand",
            "Custom door frame - right hand",
        })

    def test_15_rfq_merge_keeps_distinct_prices(self):
        """Same product/description with different unit prices must not collapse."""
        lines = self._merge_rfq_lines(
            {"line_name": "Vendor-priced component", "price": 25.0, "qty": 1.0},
            {"line_name": "Vendor-priced component", "price": 31.5, "qty": 2.0},
        )

        self.assertEqual(len(lines), 2)
        self.assertEqual(sorted(lines.mapped("price_unit")), [25.0, 31.5])

    def test_16_rfq_merge_combines_true_duplicate_lines(self):
        """Identical merge-safe lines should still combine into one RFQ line."""
        lines = self._merge_rfq_lines(
            {"line_name": "Standard component", "price": 25.0, "qty": 1.0},
            {"line_name": "Standard component", "price": 25.0, "qty": 2.0},
        )

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.name, "Standard component")
        self.assertEqual(lines.price_unit, 25.0)
        self.assertEqual(lines.product_qty, 3.0)

    def test_17_rfq_merge_clears_different_procurement_group_keys(self):
        lines = self._merge_rfq_lines(
            {
                "line_name": "Source A component",
                "price": 25.0,
                "group_key": "op:101",
            },
            {
                "line_name": "Source B component",
                "price": 25.0,
                "group_key": "op:202",
            },
        )

        self.assertFalse(lines.order_id.unico_po_group_key)

    def test_18_rfq_merge_preserves_shared_procurement_group_key(self):
        lines = self._merge_rfq_lines(
            {
                "line_name": "Shared source component A",
                "price": 25.0,
                "group_key": "so:303",
            },
            {
                "line_name": "Shared source component B",
                "price": 25.0,
                "group_key": "so:303",
            },
        )

        self.assertEqual(lines.order_id.unico_po_group_key, "so:303")

    # ------------------------------------------------------------------
    # Sale Order Status filtering + default "Ready to Order" RFQ filter
    # ------------------------------------------------------------------
    def _make_purchase(self, origin):
        return self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "origin": origin,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 1.0,
                "price_unit": 30.0,
            })],
        })

    def _make_quotation(self, qty=8.0):
        return self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "commitment_date": fields.Datetime.now() + timedelta(days=1),
            "order_line": [(0, 0, self._line_vals(self.product, qty))],
        })

    def _ready_to_order(self, candidates):
        """The domain behind the default RFQ filter."""
        return self.env["purchase.order"].search([
            ("id", "in", candidates.ids),
            ("unico_sale_order_status", "=", "sale"),
        ])

    def test_19_confirmed_sale_shows_in_the_default_filter(self):
        purchase = self._make_purchase(self._confirm_sale().name)

        self.assertEqual(self._ready_to_order(purchase), purchase)

    def test_19b_filter_handles_the_line_suffix_origin(self):
        """Special-order RFQs stamp "<sale> / <line>" onto the origin."""
        sale = self._confirm_sale()

        purchase = self._make_purchase(f"{sale.name} / 3")

        self.assertEqual(self._ready_to_order(purchase), purchase)

    def test_19c_unapproved_quote_is_hidden(self):
        """David's complaint: draft quotes must not clog the RFQ list."""
        quote = self._make_quotation()
        purchase = self._make_purchase(quote.name)

        self.assertEqual(quote.state, "draft")
        self.assertFalse(self._ready_to_order(purchase))
        self.assertEqual(purchase.unico_sale_order_status, "Quotation")

    def test_19d_cancelled_sale_is_hidden(self):
        sale = self._confirm_sale()
        purchase = self._make_purchase(sale.name)
        sale._action_cancel()

        self.assertFalse(self._ready_to_order(purchase))
        self.assertEqual(purchase.unico_sale_order_status, "Cancelled")

    def test_20_replenishment_purchase_is_hidden(self):
        purchase = self._make_purchase("OP/00016, OP/00019")

        self.assertFalse(self._ready_to_order(purchase))
        self.assertEqual(purchase.unico_sale_order_status, "N/A")

    def test_20b_purchase_with_no_origin_is_hidden(self):
        purchase = self._make_purchase(False)

        self.assertFalse(self._ready_to_order(purchase))

    def test_20c_multiple_sales_count_when_one_is_confirmed(self):
        """"If multiple then also valid" — one live sale is still real demand."""
        confirmed = self._confirm_sale()
        cancelled = self._confirm_sale()
        cancelled._action_cancel()

        purchase = self._make_purchase(f"{confirmed.name}, {cancelled.name}")

        self.assertEqual(purchase.unico_sale_order_status, "Multiple Sales Orders")
        self.assertEqual(self._ready_to_order(purchase), purchase)

    def test_20d_multiple_sales_all_dead_stay_hidden(self):
        first = self._confirm_sale()
        second = self._confirm_sale()
        (first | second)._action_cancel()

        purchase = self._make_purchase(f"{first.name}, {second.name}")

        self.assertFalse(self._ready_to_order(purchase))

    def test_21_confirming_a_quote_surfaces_its_rfq_immediately(self):
        """Nothing is stored, so there is no sync step to go stale."""
        quote = self._make_quotation()
        purchase = self._make_purchase(quote.name)
        self.assertFalse(self._ready_to_order(purchase))

        quote.action_confirm()

        self.assertEqual(self._ready_to_order(purchase), purchase)

    def test_21b_status_filter_separates_the_buckets(self):
        confirmed = self._make_purchase(self._confirm_sale().name)
        quoted = self._make_purchase(self._make_quotation().name)
        replenishment = self._make_purchase("OP/00016")
        candidates = confirmed | quoted | replenishment

        self.assertEqual(self._ready_to_order(candidates), confirmed)
        self.assertEqual(
            self.env["purchase.order"].search([
                ("id", "in", candidates.ids),
                ("unico_sale_order_status", "in", ["draft", "sent"]),
            ]),
            quoted,
        )
        self.assertEqual(
            self.env["purchase.order"].search([
                ("id", "in", candidates.ids),
                ("unico_has_linked_sale", "=", False),
            ]),
            replenishment,
        )

    def test_21c_unsupported_operator_is_rejected(self):
        with self.assertRaises(UserError):
            self.env["purchase.order"].search([
                ("unico_sale_order_status", "ilike", "sale"),
            ])

    def test_22_rfq_action_defaults_to_ready_to_order(self):
        context = literal_eval(self.env.ref("purchase.purchase_rfq").context)

        self.assertEqual(context.get("search_default_filter_unico_sale_confirmed"), 1)
        # The core key must survive replacing the action's context wholesale.
        self.assertTrue(context.get("quotation_only"))

    def test_22b_purchase_order_action_defaults_to_ready_to_order(self):
        context = literal_eval(self.env.ref("purchase.purchase_form_action").context)

        self.assertEqual(context.get("search_default_filter_unico_sale_confirmed"), 1)

    def test_23_search_views_expose_the_status_filters(self):
        """Both Purchase lists use a different search view; the default only
        resolves if the filter is registered in each."""
        for view_xmlid in (
            "purchase.view_purchase_order_filter",
            "purchase.purchase_order_view_search",
        ):
            arch = self.env["purchase.order"].get_view(
                self.env.ref(view_xmlid).id, "search",
            )["arch"]
            for filter_name in (
                "filter_unico_sale_confirmed",
                "filter_unico_sale_quotation",
                "filter_unico_sale_cancelled",
                "filter_unico_sale_none",
            ):
                self.assertIn(filter_name, arch, f"{filter_name} / {view_xmlid}")
