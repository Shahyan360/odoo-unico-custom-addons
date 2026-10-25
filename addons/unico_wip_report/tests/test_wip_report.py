# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWipReport(TransactionCase):
    """Covers tracker lifecycle, the stage model, automatic detection and the
    manual pin that overrides it, sale <-> purchase matching, the financial
    roll-up, urgency, and the dashboard payload."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaleOrder = cls.env["sale.order"]
        cls.PurchaseOrder = cls.env["purchase.order"]
        cls.Item = cls.env["unico.wip.item"]
        cls.Stage = cls.env["unico.wip.stage"]

        cls.customer = cls.env["res.partner"].create({"name": "WIP Test Customer"})
        cls.vendor = cls.env["res.partner"].create({"name": "WIP Test Vendor"})
        cls.other_vendor = cls.env["res.partner"].create({"name": "WIP Other Vendor"})
        tmpl = cls.env["product.template"].create({
            "name": "WIP Test Door",
            "type": "consu",
            "is_storable": True,
            "list_price": 500.0,
            "standard_price": 300.0,
        })
        cls.product = tmpl.product_variant_id

        cls.stage_confirmed = cls.env.ref("unico_wip_report.wip_stage_confirmed")
        cls.stage_purchasing = cls.env.ref("unico_wip_report.wip_stage_purchasing")
        cls.stage_shipping = cls.env.ref("unico_wip_report.wip_stage_shipping")
        cls.stage_receipt = cls.env.ref("unico_wip_report.wip_stage_receipt")
        cls.stage_on_hold = cls.env.ref("unico_wip_report.wip_stage_on_hold")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    # The test product is stockable with no stock on hand, so unico_delivery's
    # _check_unico_purchase_note_for_shortfall requires a sourcing note on every
    # line before the order will save. Same convention as unico_materials_report.
    PURCHASE_NOTE = "Source from vendor"

    def _order_line(self, qty=2):
        return (0, 0, {
            "product_id": self.product.id,
            "product_uom_qty": qty,
            "unico_purchase_note": self.PURCHASE_NOTE,
        })

    def _confirm_order(self, qty=2, commitment_date=None):
        vals = {
            "partner_id": self.customer.id,
            "order_line": [self._order_line(qty)],
        }
        if commitment_date:
            vals["commitment_date"] = commitment_date
        order = self.SaleOrder.create(vals)
        order.action_confirm()
        return order

    def _item(self, order):
        return self.Item.search([("order_id", "=", order.id)], limit=1)

    def _make_purchase(self, order, vendor=None, state=None, origin=None):
        """A purchase raised for ``order``, linked the way the real flows link:
        by naming the sale on the origin."""
        purchase = self.PurchaseOrder.create({
            "partner_id": (vendor or self.vendor).id,
            "origin": origin if origin is not None else order.name,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 2,
                "price_unit": 300.0,
            })],
        })
        if state == "purchase":
            purchase.button_confirm()
        self._item(order)._refresh()
        return purchase

    # ------------------------------------------------------------------
    # Tracker lifecycle
    # ------------------------------------------------------------------
    def test_quotation_has_no_tracker(self):
        order = self.SaleOrder.create({
            "partner_id": self.customer.id,
            "order_line": [self._order_line(1)],
        })
        self.assertFalse(self._item(order))

    def test_confirming_creates_the_tracker(self):
        order = self._confirm_order()
        item = self._item(order)
        self.assertTrue(item)
        self.assertTrue(item.active)
        self.assertEqual(item.stage_id, self.stage_confirmed)
        # Responsible defaults to the order's salesperson.
        self.assertEqual(item.owner_id, order.user_id)

    def test_tracker_is_unique_per_order(self):
        order = self._confirm_order()
        order._unico_ensure_wip_items()
        order._unico_ensure_wip_items()
        self.assertEqual(
            self.Item.with_context(active_test=False).search_count(
                [("order_id", "=", order.id)]), 1)

    def test_sync_archives_cancelled_orders(self):
        order = self._confirm_order()
        self.assertTrue(self._item(order).active)
        order._action_cancel()
        self.SaleOrder._unico_sync_wip_items()
        item = self.Item.with_context(active_test=False).search(
            [("order_id", "=", order.id)])
        self.assertFalse(item.active, "A cancelled order should leave the board")

    def test_facts_are_related_not_copied(self):
        """The tracker must not hold a stale second copy of order data."""
        order = self._confirm_order()
        item = self._item(order)
        new_partner = self.env["res.partner"].create({"name": "Reassigned"})
        order.partner_id = new_partner
        self.assertEqual(item.partner_id, new_partner)

    # ------------------------------------------------------------------
    # Stage model
    # ------------------------------------------------------------------
    def test_two_stages_cannot_share_an_automatic_rule(self):
        with self.assertRaises(ValidationError):
            self.Stage.create({"name": "Duplicate Rule", "auto_token": "purchasing"})

    def test_manual_only_stage_has_no_rule(self):
        self.assertFalse(self.stage_on_hold.auto_token)
        self.assertNotIn(
            self.stage_on_hold,
            self.Stage._unico_stage_by_token().values(),
            "A stage with no rule must never be a detector target",
        )

    def test_stage_by_token_maps_every_rule(self):
        mapping = self.Stage._unico_stage_by_token()
        self.assertEqual(mapping["confirmed"], self.stage_confirmed)
        self.assertEqual(mapping["purchasing"], self.stage_purchasing)
        self.assertEqual(mapping["shipping"], self.stage_shipping)

    # ------------------------------------------------------------------
    # Automatic detection
    # ------------------------------------------------------------------
    def test_open_rfq_detects_purchasing(self):
        order = self._confirm_order()
        self._make_purchase(order)
        item = self._item(order)
        self.assertEqual(item.stage_id, self.stage_purchasing)
        self.assertEqual(item.po_count, 1)
        self.assertIn(self.vendor.name, item.supplier_names)

    def test_confirmed_po_detects_shipping(self):
        order = self._confirm_order()
        purchase = self._make_purchase(order, state="purchase")
        self.assertNotEqual(purchase.receipt_status, "full")
        self.assertEqual(self._item(order).stage_id, self.stage_shipping)

    def test_stage_is_the_bottleneck_not_the_furthest(self):
        """An order with BOTH an open RFQ and a confirmed PO reads Purchasing:
        the earliest rung with open work wins."""
        order = self._confirm_order()
        self._make_purchase(order, state="purchase")
        self._make_purchase(order, vendor=self.other_vendor)
        item = self._item(order)
        self.assertEqual(item.stage_id, self.stage_purchasing)
        self.assertEqual(item.po_count, 2)

    def test_cancelled_purchase_is_ignored(self):
        order = self._confirm_order()
        purchase = self._make_purchase(order)
        purchase.button_cancel()
        item = self._item(order)
        item._refresh()
        self.assertEqual(item.stage_id, self.stage_confirmed)
        self.assertEqual(item.po_count, 0)

    def test_supplier_names_are_deduplicated(self):
        order = self._confirm_order()
        self._make_purchase(order)
        self._make_purchase(order)
        item = self._item(order)
        self.assertEqual(item.po_count, 2)
        self.assertEqual(item.supplier_names, self.vendor.name)

    # ------------------------------------------------------------------
    # Manual pin vs automatic detection
    # ------------------------------------------------------------------
    def test_moving_a_card_pins_the_stage(self):
        order = self._confirm_order()
        item = self._item(order)
        self.assertFalse(item.stage_override)
        item.stage_id = self.stage_on_hold          # what a kanban drag does
        self.assertTrue(item.stage_override, "A manual move must pin the stage")

    def test_detector_does_not_stomp_a_pinned_stage(self):
        order = self._confirm_order()
        item = self._item(order)
        item.stage_id = self.stage_on_hold
        # Raise an RFQ: the detector would otherwise move this to Purchasing.
        self._make_purchase(order)
        item._refresh()
        self.assertEqual(item.stage_id, self.stage_on_hold)
        self.assertEqual(item.auto_stage_id, self.stage_purchasing)
        self.assertTrue(item.stage_disagrees)

    def test_unpinning_restores_the_detected_stage(self):
        order = self._confirm_order()
        item = self._item(order)
        item.stage_id = self.stage_on_hold
        self._make_purchase(order)
        item.action_clear_stage_override()
        self.assertFalse(item.stage_override)
        self.assertEqual(item.stage_id, self.stage_purchasing)
        self.assertFalse(item.stage_disagrees)

    def test_pinned_to_the_detected_stage_does_not_disagree(self):
        order = self._confirm_order()
        item = self._item(order)
        item.stage_id = self.stage_confirmed  # same as detected
        self.assertTrue(item.stage_override)
        self.assertFalse(item.stage_disagrees)

    # ------------------------------------------------------------------
    # Sale <-> purchase matching
    # ------------------------------------------------------------------
    def test_origin_matching_is_token_exact(self):
        """``S00001`` must not pick up a purchase raised for ``S00001X`` — the
        naive substring match this replaces would have."""
        order = self._confirm_order()
        self._make_purchase(order, origin="%sX" % order.name)
        item = self._item(order)
        self.assertEqual(item.po_count, 0)
        self.assertEqual(item.stage_id, self.stage_confirmed)

    def test_origin_matching_handles_line_suffixed_origins(self):
        """CPQ special-component RFQs stamp "<order> / <line>" — still a match."""
        order = self._confirm_order()
        self._make_purchase(order, origin="%s / 1" % order.name)
        item = self._item(order)
        self.assertEqual(item.po_count, 1)
        self.assertEqual(item.stage_id, self.stage_purchasing)

    def test_purchase_map_is_batched_per_order(self):
        order_a = self._confirm_order()
        order_b = self._confirm_order()
        self._make_purchase(order_a)
        purchase_map = (order_a | order_b)._unico_wip_purchase_map()
        self.assertEqual(len(purchase_map[order_a.id]), 1)
        self.assertEqual(len(purchase_map[order_b.id]), 0)

    # ------------------------------------------------------------------
    # Products + financials
    # ------------------------------------------------------------------
    def test_product_summary_and_estimated_cost(self):
        order = self._confirm_order(qty=3)
        item = self._item(order)
        self.assertIn(self.product.display_name, item.product_summary)
        self.assertTrue(item.product_summary.startswith("3 x"))
        # Cost is on the ORDERED quantity, so it is meaningful before delivery.
        self.assertAlmostEqual(
            item.estimated_cost, order.order_line.purchase_price * 3, places=2)

    def test_section_lines_are_excluded_from_summary(self):
        order = self.SaleOrder.create({
            "partner_id": self.customer.id,
            "order_line": [
                (0, 0, {"display_type": "line_section", "name": "A section header"}),
                self._order_line(1),
            ],
        })
        order.action_confirm()
        item = self._item(order)
        self.assertNotIn("A section header", item.product_summary)
        self.assertIn(self.product.display_name, item.product_summary)

    # ------------------------------------------------------------------
    # Schedule, lateness, urgency
    # ------------------------------------------------------------------
    def test_overdue_order(self):
        past = fields.Datetime.now() - timedelta(days=4)
        item = self._item(self._confirm_order(commitment_date=past))
        self.assertEqual(item.urgency, "overdue")
        self.assertEqual(item.expected_date, past)
        # Bounded rather than exact: the promise date is bucketed in the user's
        # timezone, so a 4-day-old datetime lands on 3 or 4 depending on tz.
        self.assertIn(item.delay_days, (3, 4))

    def test_at_risk_order(self):
        soon = fields.Datetime.now() + timedelta(days=1)
        item = self._item(self._confirm_order(commitment_date=soon))
        self.assertEqual(item.urgency, "at_risk")
        self.assertEqual(item.delay_days, 0)

    def test_on_track_order(self):
        later = fields.Datetime.now() + timedelta(days=30)
        item = self._item(self._confirm_order(commitment_date=later))
        self.assertEqual(item.urgency, "on_track")
        self.assertEqual(item.delay_days, 0)

    # ------------------------------------------------------------------
    # Cron + dashboard
    # ------------------------------------------------------------------
    def test_cron_picks_up_origin_linked_purchase(self):
        """The purchase is linked only by origin (a Char, no dependency path),
        so only the cron refresh can surface it."""
        order = self._confirm_order()
        self.PurchaseOrder.create({
            "partner_id": self.vendor.id,
            "origin": order.name,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 1,
                "price_unit": 300.0,
            })],
        })
        self.Item._cron_refresh_wip()
        self.assertEqual(self._item(order).stage_id, self.stage_purchasing)

    def test_cron_backfills_trackers(self):
        """An order confirmed before this module was installed still gets one."""
        order = self._confirm_order()
        self._item(order).unlink()
        self.assertFalse(self._item(order))
        self.Item._cron_refresh_wip()
        self.assertTrue(self._item(order))

    def test_dashboard_payload(self):
        order = self._confirm_order(
            commitment_date=fields.Datetime.now() - timedelta(days=2))
        item = self._item(order)
        item.write({
            "blocked": True,
            "blocker_note": "Vendor has not confirmed a ship date",
        })

        dashboard = self.Item.retrieve_unico_wip_dashboard()

        self.assertGreaterEqual(dashboard["total"], 1)
        # Stage tiles are driven by the stage records, so a stage added in the
        # UI shows up without a code change.
        labels = [tile["label"] for tile in dashboard["stages"]]
        self.assertIn(self.stage_confirmed.name, labels)
        self.assertTrue(dashboard["currency"]["decimal_places"] is not None)
        self.assertGreaterEqual(dashboard["blocked"]["count"], 1)

        watchlist = dashboard["watchlist"]
        row = next(r for r in watchlist["rows"] if r["id"] == item.id)
        self.assertIn(row["delay_days"], (1, 2))
        self.assertTrue(row["blocked"])
        self.assertEqual(row["note"], "Vendor has not confirmed a ship date")
        # The payload crosses to JS as JSON — the domain must be plain data.
        self.assertIsInstance(watchlist["domain"], list)

    def test_dashboard_tile_domains_match_their_counts(self):
        """Opening a tile must show exactly what it counted."""
        self._confirm_order()
        dashboard = self.Item.retrieve_unico_wip_dashboard()
        tiles = (dashboard["stages"] + dashboard["urgency"]
                 + dashboard["payment"] + [dashboard["blocked"], dashboard["pinned"]])
        for tile in tiles:
            self.assertEqual(
                self.Item.search_count(tile["domain"]),
                tile["count"],
                "Tile %s count disagrees with its drill-down domain" % tile["label"],
            )

    # ------------------------------------------------------------------
    # Dashboard period filter
    # ------------------------------------------------------------------
    def test_period_all_is_unbounded(self):
        self.assertEqual(self.Item._wip_period_bounds("all"), (None, None))

    def test_this_week_runs_monday_to_sunday(self):
        today = fields.Date.context_today(self.Item)
        start, end = self.Item._wip_period_bounds("this_week")
        self.assertEqual(start.weekday(), 0, "week should start on Monday")
        self.assertEqual(end.weekday(), 6, "week should end on Sunday")
        self.assertLessEqual(start, today)
        self.assertGreaterEqual(end, today)

    def test_last_week_ends_the_day_before_this_week(self):
        this_start, _this_end = self.Item._wip_period_bounds("this_week")
        last_start, last_end = self.Item._wip_period_bounds("last_week")
        self.assertEqual(last_end, this_start - timedelta(days=1))
        self.assertEqual((last_end - last_start).days, 6)

    def test_this_month_spans_the_calendar_month(self):
        today = fields.Date.context_today(self.Item)
        start, end = self.Item._wip_period_bounds("this_month")
        self.assertEqual(start.day, 1)
        self.assertEqual(start.month, today.month)
        self.assertEqual((end + timedelta(days=1)).day, 1, "end should be month-end")

    def test_last_month_ends_the_day_before_this_month(self):
        this_start, _this_end = self.Item._wip_period_bounds("this_month")
        _last_start, last_end = self.Item._wip_period_bounds("last_month")
        self.assertEqual(last_end, this_start - timedelta(days=1))

    def test_custom_period_echoes_the_given_bounds(self):
        start, end = self.Item._wip_period_bounds(
            "custom", "2026-03-01", "2026-03-15")
        self.assertEqual(fields.Date.to_string(start), "2026-03-01")
        self.assertEqual(fields.Date.to_string(end), "2026-03-15")

    def test_unknown_period_and_date_field_fall_back_safely(self):
        """Both come from the client and land in a domain — they must never be
        taken verbatim."""
        dashboard = self.Item.retrieve_unico_wip_dashboard(
            period="'; DROP TABLE", date_field="order_id.amount_total")
        self.assertEqual(dashboard["filters"]["period"], "all")
        self.assertEqual(dashboard["filters"]["date_field"], "expected_date")

    def test_period_filter_excludes_orders_outside_the_window(self):
        old = self._confirm_order(
            commitment_date=fields.Datetime.now() - timedelta(days=400))
        recent = self._confirm_order(
            commitment_date=fields.Datetime.now())

        unfiltered = self.Item.retrieve_unico_wip_dashboard()
        this_month = self.Item.retrieve_unico_wip_dashboard(period="this_month")

        in_all = self.Item.search(unfiltered["base_domain"])
        in_month = self.Item.search(this_month["base_domain"])
        self.assertIn(self._item(old), in_all)
        self.assertIn(self._item(recent), in_month)
        self.assertNotIn(
            self._item(old), in_month,
            "An order due 400 days ago is not in this month's window")

    def test_period_domain_survives_a_json_round_trip(self):
        """The domain is handed to the browser and comes back on a tile click,
        so it must contain no non-JSON types."""
        import json
        dashboard = self.Item.retrieve_unico_wip_dashboard(period="this_month")
        revived = json.loads(json.dumps(dashboard["base_domain"]))
        self.assertEqual(
            self.Item.search_count(revived),
            self.Item.search_count(dashboard["base_domain"]),
        )

    def test_filtered_tile_domains_still_match_their_counts(self):
        """The period must reach every tile, not just the headline totals."""
        self._confirm_order(commitment_date=fields.Datetime.now())
        self._confirm_order(
            commitment_date=fields.Datetime.now() - timedelta(days=400))
        dashboard = self.Item.retrieve_unico_wip_dashboard(period="this_month")
        tiles = (dashboard["stages"] + dashboard["urgency"]
                 + dashboard["payment"] + [dashboard["blocked"], dashboard["pinned"]])
        for tile in tiles:
            self.assertEqual(
                self.Item.search_count(tile["domain"]),
                tile["count"],
                "Filtered tile %s disagrees with its domain" % tile["label"],
            )
        self.assertLess(
            dashboard["total"],
            self.Item.retrieve_unico_wip_dashboard()["total"],
            "The filtered board should be smaller than the unfiltered one",
        )

    def test_order_date_filter_uses_the_order_date(self):
        order = self._confirm_order()
        item = self._item(order)
        self.assertTrue(item.order_date, "order_date should mirror date_order")
        dashboard = self.Item.retrieve_unico_wip_dashboard(
            period="this_month", date_field="order_date")
        self.assertEqual(dashboard["filters"]["date_field"], "order_date")
        self.assertIn(item, self.Item.search(dashboard["base_domain"]))
