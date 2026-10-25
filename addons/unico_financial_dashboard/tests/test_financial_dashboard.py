from datetime import datetime, time, timedelta

from odoo import fields
from odoo.fields import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.sale.tests.common import SaleCommon


# AccountTestInvoicingCommon provides accounting properties and product
# accounts. The journal helpers below still self-provision journals because a
# clean at-install DB may not have bank/general journals yet.
class TestUnicoFinancialDashboard(AccountTestInvoicingCommon, SaleCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dashboard = cls.env["unico.financial.dashboard"]

    def _week_start(self, value_date):
        # Wednesday anchor, computed independently of the implementation.
        return value_date - timedelta(days=(value_date.weekday() - 2) % 7)

    def _last_complete_week_start(self):
        today = fields.Date.context_today(self.dashboard)
        return self._week_start(today) - timedelta(weeks=1)

    def _dashboard_sale_order(self, amount, fulfillment_type, order_date,
                              payment_term=None, confirm=True, pricelist=None):
        partner = self.env["res.partner"].create({
            "name": "Dashboard Sales KPI Customer",
        })
        values = {
            "partner_id": partner.id,
            "company_id": self.env.company.id,
            "date_order": fields.Datetime.to_string(
                datetime.combine(order_date, time(hour=12))
            ),
            "fulfillment_type": fulfillment_type,
        }
        if payment_term is not None:
            values["payment_term_id"] = payment_term.id
        if pricelist is not None:
            values["pricelist_id"] = pricelist.id
        order = self.env["sale.order"].create(values)
        order.order_line = [
            Command.create({
                "product_id": self.service_product.id,
                "product_uom_qty": 1.0,
                "price_unit": amount,
            }),
        ]
        if confirm:
            order.action_confirm()
        if payment_term is not None:
            order.payment_term_id = payment_term
        order.date_order = fields.Datetime.to_string(
            datetime.combine(order_date, time(hour=12))
        )
        return order

    def _open_sales_kpi_cards(self):
        payload = self.dashboard.retrieve_dashboard()
        return {
            card["label"]: card
            for card in payload["open_sales_order_kpis"]["cards"]
        }

    def _set_order_create_date(self, orders, value_date):
        create_date = datetime.combine(value_date, time(hour=12))
        orders.flush_recordset()
        for order in orders:
            self.env.cr.execute(
                "UPDATE sale_order SET create_date = %s WHERE id = %s",
                (create_date, order.id),
            )
        orders.invalidate_recordset(["create_date"])

    def _post_dashboard_credit_note(self, order, amount, with_rma):
        rma = self.env["rma.main"]
        if with_rma:
            rma = rma.create({"sale_order": order.id})
        credit = self.env["account.move"].create({
            "move_type": "out_refund",
            "partner_id": order.partner_id.id,
            "sale_id": order.id,
            "rma_id": rma.id if rma else False,
            "invoice_line_ids": [Command.create({
                "product_id": self.service_product.id,
                "quantity": 1.0,
                "price_unit": amount,
                "tax_ids": [Command.clear()],
            })],
        })
        credit.action_post()
        return credit

    def test_fulfillment_conversion_uses_creation_cohorts_and_current_outcomes(self):
        current_start = self._week_start(fields.Date.to_date("2048-04-08"))
        current_end = current_start + timedelta(days=7)
        comparison_start = current_start - timedelta(days=7)
        comparison_end = current_start

        will_call_draft = self._dashboard_sale_order(
            10.0, "will_call", current_start, confirm=False)
        will_call_sent = self._dashboard_sale_order(
            20.0, "will_call", current_start, confirm=False)
        will_call_sent.action_quotation_sent()
        will_call_paid = self._dashboard_sale_order(
            100.0, "will_call", current_start)
        self._make_payment(will_call_paid, 25.0, current_start)
        delivery_unpaid = self._dashboard_sale_order(
            40.0, "delivery", current_start)
        delivery_paid = self._dashboard_sale_order(
            100.0, "delivery", current_start)
        self._make_payment(delivery_paid, 10.0, current_start)
        delivery_created_in_range = self._dashboard_sale_order(
            50.0, "delivery", current_end + timedelta(days=20))
        not_set = self._dashboard_sale_order(
            60.0, False, current_start, confirm=False)
        cancelled = self._dashboard_sale_order(
            70.0, "delivery", current_start, confirm=False)
        cancelled.action_cancel()
        created_outside_range = self._dashboard_sale_order(
            80.0, "will_call", current_start)

        current_cohort = (
            will_call_draft
            | will_call_sent
            | will_call_paid
            | delivery_unpaid
            | delivery_paid
            | delivery_created_in_range
            | not_set
            | cancelled
        )
        self._set_order_create_date(
            current_cohort, current_start + timedelta(days=1))
        self._set_order_create_date(
            created_outside_range, current_end + timedelta(days=1))

        comparison_will_call_draft = self._dashboard_sale_order(
            10.0, "will_call", comparison_start, confirm=False)
        comparison_will_call_paid = self._dashboard_sale_order(
            100.0, "will_call", comparison_start)
        self._make_payment(
            comparison_will_call_paid, 10.0, comparison_start)
        comparison_delivery_unpaid = self._dashboard_sale_order(
            20.0, "delivery", comparison_start)
        comparison_cohort = (
            comparison_will_call_draft
            | comparison_will_call_paid
            | comparison_delivery_unpaid
        )
        self._set_order_create_date(
            comparison_cohort, comparison_start + timedelta(days=1))

        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(current_start),
            date_end=fields.Date.to_string(current_end - timedelta(days=1)),
            comparison_mode="custom",
            comparison_date_start=fields.Date.to_string(comparison_start),
            comparison_date_end=fields.Date.to_string(
                comparison_end - timedelta(days=1)),
        )
        analysis = payload["fulfillment_conversion"]
        rows = {row["id"]: row for row in analysis["rows"]}

        self.assertEqual(
            [row["id"] for row in analysis["rows"]],
            ["will_call", "delivery", "not_set"],
        )
        self.assertEqual(analysis["date_start"], payload["date_start"])
        self.assertEqual(analysis["date_end"], payload["date_end"])
        self.assertEqual(
            analysis["comparison_date_start"],
            payload["comparison_date_start"],
        )
        self.assertEqual(
            analysis["comparison_date_end"],
            payload["comparison_date_end"],
        )

        will_call = rows["will_call"]
        self.assertEqual(will_call["current_cohort_count"], 3)
        self.assertEqual(will_call["current_confirmed_count"], 1)
        self.assertEqual(will_call["current_paid_count"], 1)
        self.assertAlmostEqual(
            will_call["current_quote_sales"], 130.0, places=2)
        self.assertAlmostEqual(
            will_call["current_confirmed_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["current_paid_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["comparison_quote_sales"], 110.0, places=2)
        self.assertAlmostEqual(
            will_call["comparison_confirmed_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["comparison_paid_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["current_quote_to_sale_rate"], 33.33, places=2)
        self.assertAlmostEqual(
            will_call["current_sale_to_paid_rate"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["comparison_quote_to_sale_rate"], 50.0, places=2)
        self.assertAlmostEqual(
            will_call["comparison_sale_to_paid_rate"], 100.0, places=2)
        self.assertAlmostEqual(
            will_call["quote_to_sale_delta_points"], -16.67, places=2)
        self.assertAlmostEqual(
            will_call["sale_to_paid_delta_points"], 0.0, places=2)

        delivery = rows["delivery"]
        self.assertEqual(delivery["current_cohort_count"], 4)
        self.assertEqual(delivery["current_confirmed_count"], 3)
        self.assertEqual(delivery["current_paid_count"], 1)
        self.assertAlmostEqual(
            delivery["current_quote_sales"], 260.0, places=2)
        self.assertAlmostEqual(
            delivery["current_confirmed_sales"], 190.0, places=2)
        self.assertAlmostEqual(
            delivery["current_paid_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            delivery["comparison_quote_sales"], 20.0, places=2)
        self.assertAlmostEqual(
            delivery["comparison_confirmed_sales"], 20.0, places=2)
        self.assertAlmostEqual(
            delivery["comparison_paid_sales"], 0.0, places=2)
        self.assertAlmostEqual(
            delivery["current_quote_to_sale_rate"], 75.0, places=2)
        self.assertAlmostEqual(
            delivery["current_sale_to_paid_rate"], 33.33, places=2)
        self.assertAlmostEqual(
            delivery["comparison_quote_to_sale_rate"], 100.0, places=2)
        self.assertAlmostEqual(
            delivery["comparison_sale_to_paid_rate"], 0.0, places=2)

        not_set_row = rows["not_set"]
        self.assertEqual(not_set_row["current_cohort_count"], 1)
        self.assertEqual(not_set_row["current_confirmed_count"], 0)
        self.assertEqual(not_set_row["current_paid_count"], 0)
        self.assertEqual(not_set_row["current_quote_to_sale_rate"], 0.0)
        self.assertEqual(not_set_row["current_sale_to_paid_rate"], 0.0)

        total = analysis["current_total"]
        self.assertEqual(total["current_cohort_count"], 8)
        self.assertEqual(total["current_confirmed_count"], 4)
        self.assertEqual(total["current_paid_count"], 2)
        self.assertAlmostEqual(
            total["current_quote_sales"], 450.0, places=2)
        self.assertAlmostEqual(
            total["current_confirmed_sales"], 290.0, places=2)
        self.assertAlmostEqual(
            total["current_paid_sales"], 200.0, places=2)
        self.assertEqual(total["comparison_cohort_count"], 3)
        self.assertEqual(total["comparison_confirmed_count"], 2)
        self.assertEqual(total["comparison_paid_count"], 1)
        self.assertAlmostEqual(
            total["comparison_quote_sales"], 130.0, places=2)
        self.assertAlmostEqual(
            total["comparison_confirmed_sales"], 120.0, places=2)
        self.assertAlmostEqual(
            total["comparison_paid_sales"], 100.0, places=2)
        self.assertAlmostEqual(
            total["current_quote_to_sale_rate"], 50.0, places=2)
        self.assertAlmostEqual(
            total["current_sale_to_paid_rate"], 50.0, places=2)
        self.assertAlmostEqual(
            total["quote_to_sale_delta_points"], -16.67, places=2)
        self.assertAlmostEqual(
            total["sale_to_paid_delta_points"], 0.0, places=2)

        self.assertEqual(
            set(will_call["current_cohort_drilldown"]["domain"][0][2]),
            set((
                will_call_draft
                | will_call_sent
                | will_call_paid
            ).ids),
        )
        self.assertEqual(
            set(delivery["current_confirmed_drilldown"]["domain"][0][2]),
            set((
                delivery_unpaid
                | delivery_paid
                | delivery_created_in_range
            ).ids),
        )
        self.assertEqual(
            set(total["current_paid_drilldown"]["domain"][0][2]),
            set((will_call_paid | delivery_paid).ids),
        )
        self.assertIn(
            cancelled.id,
            total["current_cohort_drilldown"]["domain"][0][2],
        )
        self.assertNotIn(
            created_outside_range.id,
            total["current_cohort_drilldown"]["domain"][0][2],
        )

    def test_fulfillment_conversion_uses_paid_status_and_excludes_returns(self):
        current_start = self._week_start(fields.Date.to_date("2050-06-08"))
        current_end = current_start + timedelta(days=7)
        self.service_product.product_tmpl_id.invoice_policy = "order"

        fully_settled = self._dashboard_sale_order(
            100.0, "delivery", current_start)
        self._ensure_partner_receivable(fully_settled.partner_id)
        self._sale_journal()
        invoice = fully_settled._create_invoices()
        invoice.action_post()
        credit = self._post_dashboard_credit_note(
            fully_settled, 100.0, with_rma=False)
        (invoice + credit).line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        ).reconcile()
        fully_settled.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(fully_settled.unico_payment_status, "paid")

        returned = self._dashboard_sale_order(
            100.0, "delivery", current_start)
        self._post_dashboard_credit_note(returned, 100.0, with_rma=True)
        returned.invalidate_recordset(["unico_payment_status"])
        self.assertEqual(returned.unico_payment_status, "returned_credited")

        self._set_order_create_date(
            fully_settled | returned, current_start + timedelta(days=1))
        analysis = self.dashboard._fulfillment_conversion_rows(
            current_start,
            current_end,
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )
        delivery = next(
            row for row in analysis["rows"] if row["id"] == "delivery")

        self.assertEqual(delivery["current_cohort_count"], 2)
        self.assertEqual(delivery["current_confirmed_count"], 2)
        self.assertEqual(delivery["current_paid_count"], 1)
        self.assertAlmostEqual(
            delivery["current_quote_sales"], 200.0, places=2)
        self.assertAlmostEqual(
            delivery["current_confirmed_sales"], 200.0, places=2)
        self.assertAlmostEqual(
            delivery["current_paid_sales"], 100.0, places=2)
        self.assertEqual(
            delivery["current_paid_drilldown"]["domain"],
            [("id", "in", fully_settled.ids)],
        )

    def test_fulfillment_conversion_handles_empty_periods(self):
        current_start = self._week_start(fields.Date.to_date("2090-01-11"))
        analysis = self.dashboard._fulfillment_conversion_rows(
            current_start,
            current_start + timedelta(days=7),
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )

        self.assertEqual(
            [row["id"] for row in analysis["rows"]],
            ["will_call", "delivery"],
        )
        total = analysis["current_total"]
        self.assertEqual(total["current_cohort_count"], 0)
        self.assertEqual(total["current_confirmed_count"], 0)
        self.assertEqual(total["current_paid_count"], 0)
        self.assertEqual(total["current_quote_sales"], 0.0)
        self.assertEqual(total["current_confirmed_sales"], 0.0)
        self.assertEqual(total["current_paid_sales"], 0.0)
        self.assertEqual(total["current_quote_to_sale_rate"], 0.0)
        self.assertEqual(total["current_sale_to_paid_rate"], 0.0)
        self.assertEqual(total["current_cohort_drilldown"], {})
        self.assertEqual(total["current_confirmed_drilldown"], {})
        self.assertEqual(total["current_paid_drilldown"], {})

    def test_fulfillment_conversion_is_uncapped_and_respects_record_rules(self):
        current_start = self._week_start(fields.Date.to_date("2070-09-10"))
        current_end = current_start + timedelta(days=7)
        restricted_user = self.env["res.users"].create({
            "name": "Dashboard Own Orders User",
            "login": "dashboard_own_orders_user",
            "email": "dashboard.own.orders@example.com",
            "group_ids": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("account.group_account_readonly").id,
                self.env.ref("sales_team.group_sale_salesman").id,
            ])],
        })

        visible_orders = self.env["sale.order"]
        for index in range(15):
            order = self._dashboard_sale_order(
                10.0 + index,
                "will_call",
                current_start,
                confirm=False,
            )
            order.user_id = restricted_user
            visible_orders |= order

        hidden_personal_order = self._dashboard_sale_order(
            100.0, "will_call", current_start, confirm=False)
        hidden_personal_order.user_id = self.env.user
        other_company = self.env["res.company"].create({
            "name": "Dashboard Other Company",
            "currency_id": self.env.company.currency_id.id,
        })
        hidden_company_order = self._dashboard_sale_order(
            100.0, "will_call", current_start, confirm=False)
        hidden_company_order.user_id = restricted_user
        hidden_company_order.flush_recordset()
        self.env.cr.execute(
            "UPDATE sale_order SET company_id = %s WHERE id = %s",
            (other_company.id, hidden_company_order.id),
        )
        hidden_company_order.invalidate_recordset(["company_id"])

        self._set_order_create_date(
            visible_orders | hidden_personal_order | hidden_company_order,
            current_start + timedelta(days=1),
        )
        analysis = self.dashboard.with_user(
            restricted_user
        )._fulfillment_conversion_rows(
            current_start,
            current_end,
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )
        will_call = next(
            row for row in analysis["rows"] if row["id"] == "will_call")

        self.assertEqual(will_call["current_cohort_count"], 15)
        self.assertEqual(
            set(will_call["current_cohort_drilldown"]["domain"][0][2]),
            set(visible_orders.ids),
        )
        self.assertNotIn(
            hidden_personal_order.id,
            will_call["current_cohort_drilldown"]["domain"][0][2],
        )
        self.assertNotIn(
            hidden_company_order.id,
            will_call["current_cohort_drilldown"]["domain"][0][2],
        )

    def test_fulfillment_conversion_amounts_use_company_currency(self):
        current_start = self._week_start(fields.Date.to_date("2075-05-15"))
        foreign_currency = self.env.ref("base.EUR")
        foreign_currency.active = True
        self.env["res.currency.rate"].create({
            "name": fields.Date.to_string(current_start),
            "rate": 2.0,
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
        })
        pricelist = self.env["product.pricelist"].create({
            "name": "Fulfillment Conversion Foreign Currency",
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
        })
        order = self._dashboard_sale_order(
            100.0,
            "delivery",
            current_start,
            pricelist=pricelist,
        )
        self._set_order_create_date(
            order, current_start + timedelta(days=1))
        expected = foreign_currency._convert(
            100.0,
            self.env.company.currency_id,
            self.env.company,
            current_start,
        )

        analysis = self.dashboard._fulfillment_conversion_rows(
            current_start,
            current_start + timedelta(days=7),
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )
        delivery = next(
            row for row in analysis["rows"] if row["id"] == "delivery")

        self.assertNotEqual(expected, 100.0)
        self.assertAlmostEqual(
            delivery["current_quote_sales"], expected, places=2)
        self.assertAlmostEqual(
            delivery["current_confirmed_sales"], expected, places=2)
        self.assertAlmostEqual(
            analysis["current_total"]["current_confirmed_sales"],
            expected,
            places=2,
        )

    def test_default_view_has_eight_complete_weeks_plus_partial(self):
        payload = self.dashboard.retrieve_dashboard()

        self.assertEqual(len(payload["weeks"]), 9)
        self.assertEqual(len(payload["weekly"]), 9)
        self.assertTrue(payload["weeks"][-1]["partial"])
        self.assertTrue(all(not week["partial"] for week in payload["weeks"][:-1]))
        self.assertIn("range_dates", payload["summary_context"])
        self.assertIn("comparison_dates", payload["summary_context"])
        self.assertIn("snapshot_label", payload["summary_context"])
        self.assertIn("Reporting weeks", payload["summary_context"]["reporting_week_label"])

    def test_ar_aging_buckets_include_report_fields(self):
        payload = self.dashboard.retrieve_dashboard()
        buckets = payload["aging_buckets"]

        self.assertEqual(
            [bucket["label"] for bucket in buckets],
            ["Current", "1-30", "31-60", "61-90", "90+"],
        )
        for bucket in buckets:
            self.assertIn("amount", bucket)
            self.assertIn("count", bucket)
            self.assertIn("percent", bucket)
        self.assertIn("ar_aging_report", payload)

    def test_open_sales_order_kpis_use_rolling_thirty_day_window(self):
        company = self.env.company
        today = fields.Date.context_today(self.dashboard)
        before = self._open_sales_kpi_cards()

        will_call = self._dashboard_sale_order(
            100.0, "will_call", today - timedelta(days=5))
        delivery = self._dashboard_sale_order(
            200.0, "delivery", today - timedelta(days=3))
        self._dashboard_sale_order(
            999.0, "delivery", today - timedelta(days=40))
        expected_total = self.dashboard._sales_order_total(
            will_call | delivery, company)
        after = self._open_sales_kpi_cards()

        self.assertEqual(
            after["Booked Sales #"]["value"] - before["Booked Sales #"]["value"],
            2,
        )
        self.assertAlmostEqual(
            after["Booked Sales $"]["value"] - before["Booked Sales $"]["value"],
            expected_total,
            places=2,
        )
        self.assertEqual(
            after["Open Sales Orders #"]["value"] - before["Open Sales Orders #"]["value"],
            2,
        )
        self.assertAlmostEqual(
            after["Open Sales Orders $"]["value"] - before["Open Sales Orders $"]["value"],
            expected_total,
            places=2,
        )
        self.assertEqual(
            after["Open Will Call #"]["value"] - before["Open Will Call #"]["value"],
            1,
        )
        self.assertEqual(
            after["Open Deliveries #"]["value"] - before["Open Deliveries #"]["value"],
            1,
        )
        self.assertEqual(after["Average Order $"]["value_type"], "money")

    def test_open_sales_order_kpis_exclude_paid_and_invoiced_orders(self):
        company = self.env.company
        today = fields.Date.context_today(self.dashboard)
        before = self._open_sales_kpi_cards()

        open_order = self._dashboard_sale_order(
            100.0, "will_call", today - timedelta(days=1))
        paid_order = self._dashboard_sale_order(
            200.0, "delivery", today - timedelta(days=1))
        invoiced_order = self._dashboard_sale_order(
            300.0, "delivery", today - timedelta(days=1))
        self._make_payment(paid_order, 200.0, today)
        invoice = invoiced_order._create_invoices()
        invoice.action_post()
        (open_order | paid_order | invoiced_order).invalidate_recordset([
            "invoice_status",
            "unico_payment_status",
        ])

        expected_booked_total = self.dashboard._sales_order_total(
            open_order | paid_order | invoiced_order, company)
        expected_open_total = self.dashboard._sales_order_total(
            open_order, company)
        after = self._open_sales_kpi_cards()

        self.assertEqual(
            after["Booked Sales #"]["value"] - before["Booked Sales #"]["value"],
            3,
        )
        self.assertAlmostEqual(
            after["Booked Sales $"]["value"] - before["Booked Sales $"]["value"],
            expected_booked_total,
            places=2,
        )
        self.assertEqual(
            after["Open Sales Orders #"]["value"] - before["Open Sales Orders #"]["value"],
            1,
        )
        self.assertAlmostEqual(
            after["Open Sales Orders $"]["value"] - before["Open Sales Orders $"]["value"],
            expected_open_total,
            places=2,
        )

    def test_payment_term_analysis_compares_confirmed_sales_mix(self):
        current_start = self._week_start(fields.Date.to_date("2040-01-10"))
        current_end = current_start + timedelta(days=7)
        comparison_start = current_start - timedelta(days=7)
        comparison_end = current_start
        immediate = self.env.ref("account.account_payment_term_immediate")
        net_30 = self.env.ref("account.account_payment_term_30days")
        net_60 = self.env["account.payment.term"].create({
            "name": "60 Days Dashboard Test",
        })

        current_immediate = self._dashboard_sale_order(
            100.0, "will_call", current_start, immediate)
        current_net_30 = self._dashboard_sale_order(
            300.0, "delivery", current_start + timedelta(days=1), net_30)
        current_no_terms = self._dashboard_sale_order(
            100.0, "will_call", current_start + timedelta(days=2))
        current_no_terms.payment_term_id = False
        comparison_net_30 = self._dashboard_sale_order(
            200.0, "delivery", comparison_start, net_30)
        comparison_net_60 = self._dashboard_sale_order(
            800.0, "delivery", comparison_start + timedelta(days=1), net_60)

        # These records prove that only confirmed orders inside the resolved
        # selected period feed the table.
        self._dashboard_sale_order(
            900.0, "delivery", current_start + timedelta(days=3),
            immediate, confirm=False)
        cancelled = self._dashboard_sale_order(
            700.0, "delivery", current_start + timedelta(days=4), net_30)
        cancelled.action_cancel()
        self._dashboard_sale_order(
            600.0, "delivery", current_end + timedelta(days=1), immediate)

        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(current_start),
            date_end=fields.Date.to_string(current_end - timedelta(days=1)),
            comparison_mode="custom",
            comparison_date_start=fields.Date.to_string(comparison_start),
            comparison_date_end=fields.Date.to_string(
                comparison_end - timedelta(days=1)),
        )
        analysis = payload["payment_term_analysis"]
        rows = {row["id"]: row for row in analysis["rows"]}

        self.assertEqual(analysis["date_start"], payload["date_start"])
        self.assertEqual(analysis["date_end"], payload["date_end"])
        self.assertEqual(
            analysis["comparison_date_start"],
            payload["comparison_date_start"],
        )
        self.assertEqual(
            analysis["comparison_date_end"],
            payload["comparison_date_end"],
        )
        self.assertEqual(analysis["current_total"]["order_count"], 3)
        self.assertAlmostEqual(
            analysis["current_total"]["confirmed_sales"], 500.0, places=2)
        self.assertEqual(analysis["comparison_total"]["order_count"], 2)
        self.assertAlmostEqual(
            analysis["comparison_total"]["confirmed_sales"], 1000.0, places=2)
        self.assertEqual(analysis["rows"][0]["id"], net_30.id)
        self.assertEqual(analysis["rows"][-1]["id"], net_60.id)

        self.assertEqual(rows[net_30.id]["current_order_count"], 1)
        self.assertAlmostEqual(
            rows[net_30.id]["current_confirmed_sales"], 300.0, places=2)
        self.assertAlmostEqual(
            rows[net_30.id]["current_share_percent"], 60.0, places=2)
        self.assertAlmostEqual(
            rows[net_30.id]["comparison_share_percent"], 20.0, places=2)
        self.assertAlmostEqual(
            rows[net_30.id]["share_delta_points"], 40.0, places=2)
        self.assertEqual(
            rows[net_30.id]["current_drilldown"]["domain"],
            [("id", "in", current_net_30.ids)],
        )
        self.assertEqual(
            rows[net_30.id]["comparison_drilldown"]["domain"],
            [("id", "in", comparison_net_30.ids)],
        )

        self.assertEqual(rows[0]["label"], "No Payment Terms")
        self.assertEqual(
            rows[0]["current_drilldown"]["domain"],
            [("id", "in", current_no_terms.ids)],
        )
        self.assertAlmostEqual(
            rows[net_60.id]["current_share_percent"], 0.0, places=2)
        self.assertAlmostEqual(
            rows[net_60.id]["comparison_share_percent"], 80.0, places=2)
        self.assertAlmostEqual(
            rows[net_60.id]["share_delta_points"], -80.0, places=2)
        self.assertEqual(rows[net_60.id]["current_drilldown"], {})
        self.assertEqual(
            rows[net_60.id]["comparison_drilldown"]["domain"],
            [("id", "in", comparison_net_60.ids)],
        )
        self.assertEqual(
            rows[immediate.id]["current_drilldown"]["domain"],
            [("id", "in", current_immediate.ids)],
        )
        self.assertNotIn("fulfillment_analysis", payload)

    def test_payment_term_analysis_has_no_row_cap(self):
        current_start = self._week_start(fields.Date.to_date("2042-03-12"))
        payment_terms = self.env["account.payment.term"]
        for index in range(13):
            term = self.env["account.payment.term"].create({
                "name": "Dashboard Term %02d" % index,
            })
            payment_terms |= term
            self._dashboard_sale_order(
                10.0 + index,
                "will_call",
                current_start + timedelta(days=index % 7),
                term,
            )

        analysis = self.dashboard._payment_term_rows(
            current_start,
            current_start + timedelta(days=7),
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )

        self.assertTrue(payment_terms.ids)
        self.assertTrue(
            set(payment_terms.ids).issubset(
                {row["id"] for row in analysis["rows"]}
            )
        )
        self.assertGreaterEqual(len(analysis["rows"]), 13)
        self.assertEqual(analysis["current_total"]["order_count"], 13)

    def test_payment_term_analysis_converts_foreign_currency(self):
        current_start = self._week_start(fields.Date.to_date("2044-05-18"))
        foreign_currency = self.env.ref("base.EUR")
        foreign_currency.active = True
        self.env["res.currency.rate"].create({
            "name": fields.Date.to_string(current_start),
            "rate": 2.0,
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
        })
        pricelist = self.env["product.pricelist"].create({
            "name": "Dashboard Foreign Currency",
            "currency_id": foreign_currency.id,
            "company_id": self.env.company.id,
        })
        net_30 = self.env.ref("account.account_payment_term_30days")
        self._dashboard_sale_order(
            100.0,
            "delivery",
            current_start,
            net_30,
            pricelist=pricelist,
        )
        expected = foreign_currency._convert(
            100.0,
            self.env.company.currency_id,
            self.env.company,
            current_start,
        )

        analysis = self.dashboard._payment_term_rows(
            current_start,
            current_start + timedelta(days=7),
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )
        row = next(
            item for item in analysis["rows"] if item["id"] == net_30.id)

        self.assertNotEqual(expected, 100.0)
        self.assertAlmostEqual(
            row["current_confirmed_sales"], expected, places=2)
        self.assertAlmostEqual(
            analysis["current_total"]["confirmed_sales"], expected, places=2)

    def test_payment_term_analysis_handles_empty_periods(self):
        current_start = self._week_start(fields.Date.to_date("2060-08-19"))

        analysis = self.dashboard._payment_term_rows(
            current_start,
            current_start + timedelta(days=7),
            self.env.company,
            comparison_start=current_start - timedelta(days=7),
            comparison_end=current_start,
        )

        self.assertEqual(analysis["rows"], [])
        self.assertEqual(
            analysis["current_total"],
            {"order_count": 0, "confirmed_sales": 0.0},
        )
        self.assertEqual(
            analysis["comparison_total"],
            {"order_count": 0, "confirmed_sales": 0.0},
        )

    def test_weeks_run_wednesday_to_tuesday(self):
        payload = self.dashboard.retrieve_dashboard()

        for week in payload["weeks"]:
            start = fields.Date.to_date(week["start"])
            end = fields.Date.to_date(week["end"])
            self.assertEqual(start.weekday(), 2, "weeks must start on Wednesday")
            self.assertEqual(end.weekday(), 1, "weeks must end on Tuesday")
            self.assertEqual((end - start).days, 6)

    def test_current_partial_week_contains_today(self):
        today = fields.Date.context_today(self.dashboard)
        payload = self.dashboard.retrieve_dashboard()

        last = payload["weeks"][-1]
        self.assertLessEqual(fields.Date.to_date(last["start"]), today)
        self.assertGreaterEqual(fields.Date.to_date(last["end"]), today)

    def test_custom_date_range_keeps_selected_dates(self):
        anchor = self._last_complete_week_start() - timedelta(weeks=2)
        selected_start = anchor + timedelta(days=3)
        selected_end = anchor + timedelta(weeks=2, days=1)
        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(selected_start),
            date_end=fields.Date.to_string(selected_end),
        )

        self.assertEqual(payload["date_start"], fields.Date.to_string(selected_start))
        self.assertEqual(payload["date_end"], fields.Date.to_string(selected_end))
        self.assertEqual(len(payload["weekly"]), 3)
        self.assertEqual(payload["range_preset"], "custom")

    def test_custom_calendar_month_range_keeps_exact_dates(self):
        payload = self.dashboard.retrieve_dashboard(
            date_start="2026-06-01",
            date_end="2026-06-30",
        )

        self.assertEqual(payload["date_start"], "2026-06-01")
        self.assertEqual(payload["date_end"], "2026-06-30")
        self.assertEqual(payload["range_preset"], "custom")

    def test_range_capped_at_53_weeks_keeps_most_recent(self):
        end = self._last_complete_week_start() + timedelta(days=6)
        start = self._week_start(end) - timedelta(weeks=70)
        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(start),
            date_end=fields.Date.to_string(end),
        )

        self.assertEqual(len(payload["weekly"]), 53)
        self.assertEqual(payload["date_end"], fields.Date.to_string(end))
        self.assertNotIn("through", payload["summary_context"]["range_label"])
        self.assertEqual(
            payload["payment_term_analysis"]["date_start"],
            payload["date_start"],
        )
        self.assertEqual(
            payload["payment_term_analysis"]["date_end"],
            payload["date_end"],
        )
        self.assertEqual(
            payload["payment_term_analysis"]["comparison_date_start"],
            payload["comparison_date_start"],
        )
        self.assertEqual(
            payload["payment_term_analysis"]["comparison_date_end"],
            payload["comparison_date_end"],
        )

    def test_named_range_presets_keep_selected_dates(self):
        today = fields.Date.context_today(self.dashboard)
        ytd_start = today.replace(month=1, day=1)
        cases = [
            ("past_day", today, today),
            ("past_week", today - timedelta(days=6), today),
            ("past_month", today - timedelta(days=29), today),
            ("year_to_date", ytd_start, today),
            ("past_year", today - timedelta(days=364), today),
        ]

        for preset, raw_start, raw_end in cases:
            with self.subTest(preset=preset):
                payload = self.dashboard.retrieve_dashboard(range_preset=preset)
                self.assertEqual(payload["range_preset"], preset)
                self.assertEqual(payload["date_start"], fields.Date.to_string(raw_start))
                self.assertEqual(payload["date_end"], fields.Date.to_string(raw_end))
                self.assertLessEqual(len(payload["weekly"]), self.dashboard.MAX_WEEKS)

    def test_invalid_range_falls_back_to_default(self):
        today = fields.Date.context_today(self.dashboard)

        reversed_payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(today),
            date_end=fields.Date.to_string(today - timedelta(days=30)),
        )
        self.assertEqual(len(reversed_payload["weekly"]), 9)

        garbage_payload = self.dashboard.retrieve_dashboard(
            date_start="not-a-date", date_end="also-bad",
        )
        self.assertEqual(len(garbage_payload["weekly"]), 9)

    def test_comparison_period_can_be_changed(self):
        report_start = self._last_complete_week_start() - timedelta(weeks=2)
        report_end = report_start + timedelta(weeks=1, days=6)

        prior_year_payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(report_start),
            date_end=fields.Date.to_string(report_end),
            comparison_mode="same_period_last_year",
        )
        self.assertEqual(
            prior_year_payload["comparison_mode"], "same_period_last_year")
        self.assertEqual(
            fields.Date.to_date(prior_year_payload["comparison_date_start"]),
            report_start - timedelta(weeks=52),
        )
        self.assertEqual(
            fields.Date.to_date(prior_year_payload["comparison_date_end"]),
            report_end - timedelta(weeks=52),
        )

        custom_start = report_start - timedelta(weeks=10)
        custom_payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(report_start),
            date_end=fields.Date.to_string(report_end),
            comparison_mode="custom",
            comparison_date_start=fields.Date.to_string(custom_start + timedelta(days=2)),
            comparison_date_end=fields.Date.to_string(custom_start + timedelta(days=8)),
        )

        self.assertEqual(custom_payload["comparison_mode"], "custom")
        self.assertEqual(
            fields.Date.to_date(custom_payload["comparison_date_start"]),
            custom_start + timedelta(days=2),
        )
        self.assertEqual(
            fields.Date.to_date(custom_payload["comparison_date_end"]),
            custom_start + timedelta(days=8),
        )
        self.assertEqual(
            custom_payload["summary_context"]["comparison_label"],
            "vs custom comparison",
        )

    def test_confirmed_sale_margin_feeds_financial_dashboard(self):
        self.service_product.standard_price = 60.0
        order = self.empty_order
        week_start = self._last_complete_week_start()
        order.date_order = fields.Datetime.to_string(
            datetime.combine(week_start, time(hour=12))
        )
        order.order_line = [
            Command.create({
                "product_id": self.service_product.id,
                "product_uom_qty": 2.0,
                "price_unit": 100.0,
            }),
        ]
        order.action_confirm()
        order.date_order = fields.Datetime.to_string(
            datetime.combine(week_start, time(hour=12))
        )

        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(week_start),
            date_end=fields.Date.to_string(week_start + timedelta(days=6)),
        )
        row = payload["weekly"][0]

        self.assertEqual(len(payload["weekly"]), 1)
        self.assertAlmostEqual(row["confirmed_sales"], 200.0, places=2)
        self.assertAlmostEqual(row["sales"], 200.0, places=2)
        self.assertAlmostEqual(row["gross_profit"], 80.0, places=2)
        self.assertAlmostEqual(row["margin_percent"], 40.0, places=2)
        cards = {card["label"]: card for card in payload["summary_cards"]}
        self.assertAlmostEqual(cards["Booked Sales"]["value"], 200.0, places=2)
        self.assertEqual(cards["Booked Sales"]["metric_key"], "confirmed_sales")
        self.assertIn("confirmed", payload["metric_definitions"]["confirmed_sales"].lower())

    def test_summary_cards_use_selected_week_range_totals(self):
        company = self.env.company
        windows = self.dashboard._week_windows_from_start(
            self._last_complete_week_start() - timedelta(weeks=1), 2
        )
        weekly = self.dashboard._empty_weekly_rows(windows)
        weekly[0]["confirmed_sales"] = 100.0
        weekly[0]["sales"] = 100.0
        weekly[0]["gross_profit"] = 60.0
        weekly[1]["confirmed_sales"] = 200.0
        weekly[1]["sales"] = 200.0
        weekly[1]["gross_profit"] = 80.0
        range_summary = self.dashboard._range_summary(weekly)
        prior_windows = self.dashboard._prior_week_windows(windows[0]["start"], 2)
        prior_summary = self.dashboard._range_summary(
            self.dashboard._empty_weekly_rows(prior_windows)
        )
        summary_context = self.dashboard._summary_context(windows, prior_windows)
        range_domains = self.dashboard._range_domains(
            windows[0]["start"], windows[-1]["end"], company
        )
        open_domains = self.dashboard._current_open_domains(company)
        cards = self.dashboard._summary_cards(
            range_summary, prior_summary, range_domains, open_domains,
            {"amount": 0.0, "count": 0},
            {"total": 0.0, "count": 0, "rows": []},
            {"amount": 0.0, "count": 0},
            {"amount": 0.0, "count": 0},
            {"total": 0.0, "rows": []}, 2,
            summary_context=summary_context,
        )
        two_week_gross_profit = next(
            card for card in cards if card["label"] == "Estimated Gross Profit"
        )
        two_week_confirmed_sales = next(
            card for card in cards if card["label"] == "Booked Sales"
        )

        self.assertAlmostEqual(two_week_confirmed_sales["value"], 300.0, places=2)
        self.assertAlmostEqual(two_week_gross_profit["value"], 140.0, places=2)
        self.assertEqual(two_week_gross_profit["context_label"], "Period total")
        self.assertEqual(
            two_week_gross_profit["period_label"],
            summary_context["range_dates_short"],
        )
        self.assertEqual(
            two_week_gross_profit["delta_label"],
            "vs %s" % summary_context["comparison_dates_short"],
        )

    def test_inventory_value_is_aggregated_by_stock_valuation_account(self):
        company = self.env.company
        location = self.env.ref("stock.stock_location_stock")
        account_a = self._test_account(
            "TESTINVA", "asset_current", "Test Inventory Account A")
        account_b = self._test_account(
            "TESTINVB", "asset_current", "Test Inventory Account B")
        category_a = self.env["product.category"].create({
            "name": "Dashboard Inventory A",
        })
        category_b = self.env["product.category"].create({
            "name": "Dashboard Inventory B",
        })
        category_a = category_a.with_company(company)
        category_b = category_b.with_company(company)
        category_a.property_cost_method = "standard"
        category_a.property_valuation = "real_time"
        category_a.property_stock_valuation_account_id = account_a
        category_b.property_cost_method = "standard"
        category_b.property_valuation = "real_time"
        category_b.property_stock_valuation_account_id = account_b
        product_a = self.env["product.product"].create({
            "name": "Dashboard Stock A",
            "type": "consu",
            "is_storable": True,
            "categ_id": category_a.id,
            "standard_price": 10.0,
        })
        product_b = self.env["product.product"].create({
            "name": "Dashboard Stock B",
            "type": "consu",
            "is_storable": True,
            "categ_id": category_b.id,
            "standard_price": 7.0,
        })
        before = self.dashboard._inventory_value_payload(company)
        before_by_account = {
            row["id"]: row["amount"] for row in before["rows"]
        }

        self.env["stock.quant"]._update_available_quantity(
            product_a, location, 5.0)
        self.env["stock.quant"]._update_available_quantity(
            product_b, location, 2.0)
        self.env["stock.quant"].flush_model([
            "product_id", "location_id", "owner_id", "company_id",
            "quantity", "reserved_quantity",
        ])
        (product_a | product_b).invalidate_recordset([
            "qty_available", "total_value",
        ])

        payload = self.dashboard._inventory_value_payload(company)
        by_account = {row["id"]: row for row in payload["rows"]}
        delta_a = by_account[account_a.id]["amount"] - before_by_account.get(
            account_a.id, 0.0)
        delta_b = by_account[account_b.id]["amount"] - before_by_account.get(
            account_b.id, 0.0)

        self.assertAlmostEqual(delta_a, 50.0, places=2)
        self.assertAlmostEqual(delta_b, 14.0, places=2)
        self.assertAlmostEqual(
            payload["total"] - before["total"], 64.0, places=2)
        dashboard_payload = self.dashboard.retrieve_dashboard()
        self.assertTrue(
            dashboard_payload["inventory_value"]["history"]["deferred"])
        self.assertFalse(
            dashboard_payload["inventory_value"]["history"]["series"])

        history_payload = self.dashboard.retrieve_inventory_history(
            range_preset="custom",
            date_start=dashboard_payload["date_start"],
            date_end=dashboard_payload["date_end"],
        )
        history = history_payload["history"]
        latest_index = len(history["points"]) - 1
        series_by_key = {series["key"]: series for series in history["series"]}

        self.assertIn("total", series_by_key)
        self.assertIn(str(account_a.id), series_by_key)
        self.assertIn(str(account_b.id), series_by_key)
        self.assertAlmostEqual(
            series_by_key["total"]["values"][latest_index],
            payload["total"],
            places=2,
        )
        self.assertAlmostEqual(
            series_by_key[str(account_a.id)]["values"][latest_index],
            by_account[account_a.id]["amount"],
            places=2,
        )
        self.assertAlmostEqual(
            series_by_key[str(account_b.id)]["values"][latest_index],
            by_account[account_b.id]["amount"],
            places=2,
        )

    def test_inventory_history_uses_month_end_points_for_long_ranges(self):
        start = fields.Date.to_date("2026-01-01")
        end = fields.Date.to_date("2026-06-30")
        windows = self.dashboard._date_windows_from_range(start, end)
        history_windows, granularity = self.dashboard._inventory_history_windows(
            windows)

        self.assertEqual(granularity, "monthly")
        self.assertLess(len(history_windows), len(windows))
        self.assertEqual(
            len({
                (window["end"] - timedelta(days=1)).replace(day=1)
                for window in windows
            }),
            len(history_windows),
        )

    def _test_account(self, code, account_type, name, reconcile=False):
        account = self.env["account.account"].search([
            ("code", "=", code),
            ("company_ids", "in", self.env.company.ids),
        ], limit=1)
        if account:
            return account
        return self.env["account.account"].create({
            "name": name,
            "code": code,
            "account_type": account_type,
            "reconcile": reconcile,
            "company_ids": [Command.link(self.env.company.id)],
        })

    def _ensure_partner_receivable(self, partner):
        receivable = self.company_data.get("default_account_receivable")
        if not receivable:
            receivable = self._test_account(
                "TESTRCV", "asset_receivable", "Test Receivable",
                reconcile=True)
        partner.property_account_receivable_id = receivable

    def _ensure_partner_payable(self, partner):
        payable = self.company_data.get("default_account_payable")
        if not payable:
            payable = self._test_account(
                "TESTPAY", "liability_payable", "Test Payable",
                reconcile=True)
        partner.property_account_payable_id = payable

    def _sale_journal(self):
        journal = self.company_data.get("default_journal_sale")
        if journal:
            return journal
        journal = self.env["account.journal"].search([
            ("type", "=", "sale"),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        if journal:
            return journal
        income_account = self.company_data.get("default_account_revenue")
        if not income_account:
            income_account = self._test_account(
                "TESTREV", "income", "Test Revenue")
        return self.env["account.journal"].create({
            "name": "Test Sales",
            "type": "sale",
            "code": "TSAL",
            "default_account_id": income_account.id,
            "company_id": self.env.company.id,
        })

    def _misc_journal(self):
        journal = self.company_data.get("default_journal_misc")
        if journal:
            return journal
        journal = self.env["account.journal"].search([
            ("type", "=", "general"),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        if journal:
            return journal
        return self.env["account.journal"].create({
            "name": "Test Miscellaneous",
            "type": "general",
            "code": "TGEN",
            "company_id": self.env.company.id,
        })

    def _bank_journal(self):
        journal = self.company_data.get("default_journal_bank")
        if not journal:
            journal = self.env["account.journal"].search([
                ("type", "=", "bank"),
                ("company_id", "=", self.env.company.id),
            ], limit=1)
        liquidity_account = self._test_account(
            "TESTBNK", "asset_cash", "Test Bank Liquidity", reconcile=True)
        outstanding_account = self._test_account(
            "TESTOUTR", "asset_current", "Test Outstanding Receipts",
            reconcile=True)
        if not journal:
            journal = self.env["account.journal"].create({
                "name": "Test Bank",
                "type": "bank",
                "code": "TBNK",
                "default_account_id": liquidity_account.id,
                "company_id": self.env.company.id,
            })
        elif not journal.default_account_id:
            journal.default_account_id = liquidity_account

        method_line = journal.inbound_payment_method_line_ids[:1]
        if method_line:
            method_line.payment_account_id = outstanding_account
        else:
            method = self.env.ref("account.account_payment_method_manual_in")
            self.env["account.payment.method.line"].create({
                "name": method.name,
                "payment_method_id": method.id,
                "journal_id": journal.id,
                "payment_account_id": outstanding_account.id,
            })
        return journal

    def _make_payment(self, order, amount, pay_date):
        self._ensure_partner_receivable(order.partner_id)
        self._sale_journal()
        journal = self._bank_journal()
        method_line = journal.inbound_payment_method_line_ids[:1]
        payment = self.env["account.payment"].create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": order.partner_id.id,
            "amount": amount,
            "date": pay_date,
            "journal_id": journal.id,
            "payment_method_line_id": method_line.id,
            "sale_order_id": order.id,
        })
        payment.action_post()
        return payment

    def test_cash_collected_splits_pre_invoice_from_invoiced(self):
        week_start = self._last_complete_week_start()
        order = self.empty_order
        order.order_line = [
            Command.create({
                "product_id": self.service_product.id,
                "product_uom_qty": 1.0,
                "price_unit": 100.0,
            }),
        ]
        order.action_confirm()

        # Deposit taken before any invoice exists: pre-invoice cash.
        deposit = self._make_payment(order, 40.0, week_start)

        order.order_line.qty_delivered = 1.0
        invoice = order._create_invoices()
        invoice.invoice_date = week_start + timedelta(days=1)
        invoice.action_post()

        # Payment after the invoice date settles recognized revenue.
        settlement = self._make_payment(
            order, 60.0, week_start + timedelta(days=2))

        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(week_start),
            date_end=fields.Date.to_string(week_start + timedelta(days=6)),
        )
        row = payload["weekly"][0]

        self.assertAlmostEqual(row["collected_pre_invoice"], 40.0, places=2)
        self.assertAlmostEqual(row["collected_invoiced"], 60.0, places=2)
        self.assertAlmostEqual(row["cash_collected"], 100.0, places=2)
        self.assertNotIn("collected_cash", row)

        cards = {card["label"]: card for card in payload["summary_cards"]}
        self.assertIn("Cash Collected", cards)
        self.assertIn("Pre-Invoice Collections", cards)
        self.assertNotIn("Collected Cash", cards)
        pre_domain = cards["Pre-Invoice Collections"]["drilldown"]["domain"]
        self.assertIn(deposit.id, pre_domain[0][2])
        self.assertNotIn(settlement.id, pre_domain[0][2])

    def test_revenue_relabelled_as_invoiced(self):
        payload = self.dashboard.retrieve_dashboard()

        labels = [card["label"] for card in payload["summary_cards"]]
        self.assertIn("Invoiced Revenue", labels)
        self.assertNotIn("Revenue", labels)
        self.assertIn("posted customer invoice revenue", payload["metric_definitions"]["revenue"].lower())
        self.assertNotIn("collected_cash", payload["metric_definitions"])

        cards = {card["label"]: card for card in payload["summary_cards"]}
        self.assertEqual(cards["Invoiced Revenue"]["metric_key"], "revenue")
        self.assertEqual(
            cards["Pre-Invoice Collections"]["metric_key"], "collected_pre_invoice")
        for card in payload["summary_cards"]:
            self.assertIn("metric_key", card)

    def test_payment_exception_status_labels_are_business_readable(self):
        self.assertEqual(
            self.dashboard._payment_exception_status_label("not_paid"),
            "Invoice Not Paid",
        )
        self.assertEqual(
            self.dashboard._payment_exception_status_label("partial"),
            "Invoice Partially Paid",
        )
        self.assertEqual(
            self.dashboard._stripe_exception_status_label("failed"),
            "Stripe Payment Failed",
        )
        self.assertEqual(
            self.dashboard._stripe_exception_status_label("processing"),
            "Stripe Payment Processing",
        )

    def _account_by_type(self, account_type, code):
        account = self.env["account.account"].search([
            ("account_type", "=", account_type),
            ("company_ids", "in", self.env.company.ids),
        ], limit=1)
        if not account:
            account = self.env["account.account"].create({
                "name": "Test %s" % account_type,
                "code": code,
                "account_type": account_type,
            })
        return account

    def _post_entry(self, entry_date, amount, account, product=None,
                    credit=False, post=True):
        offset_account = self._account_by_type("asset_current", "TESTCUR")
        journal = self._misc_journal()
        move = self.env["account.move"].create({
            "move_type": "entry",
            "date": entry_date,
            "journal_id": journal.id,
            "line_ids": [
                Command.create({
                    "account_id": account.id,
                    "debit": 0.0 if credit else amount,
                    "credit": amount if credit else 0.0,
                    "product_id": product.id if product else False,
                    "name": "test line",
                }),
                Command.create({
                    "account_id": offset_account.id,
                    "debit": amount if credit else 0.0,
                    "credit": 0.0 if credit else amount,
                    "name": "test offset",
                }),
            ],
        })
        if post:
            move.action_post()
        return move

    def _post_partner_residual(self, entry_date, signed_amount, account,
                               partner, ref, due_date=None):
        offset_account = self._account_by_type("asset_current", "TESTCUR")
        journal = self._misc_journal()
        amount = abs(signed_amount)
        move = self.env["account.move"].create({
            "move_type": "entry",
            "date": entry_date,
            "ref": ref,
            "journal_id": journal.id,
            "line_ids": [
                Command.create({
                    "account_id": account.id,
                    "partner_id": partner.id,
                    "date_maturity": due_date or entry_date,
                    "debit": amount if signed_amount > 0 else 0.0,
                    "credit": amount if signed_amount < 0 else 0.0,
                    "name": ref,
                }),
                Command.create({
                    "account_id": offset_account.id,
                    "debit": amount if signed_amount < 0 else 0.0,
                    "credit": amount if signed_amount > 0 else 0.0,
                    "name": "%s offset" % ref,
                }),
            ],
        })
        move.action_post()
        residual_line = move.line_ids.filtered(
            lambda line: line.account_id == account
        )
        return move, residual_line[:1]

    def test_ar_aging_groups_by_customer_and_uses_current_tags(self):
        company = self.env.company
        today = fields.Date.context_today(self.dashboard)
        credit_hold = self.env["res.partner.category"].create({
            "name": "Credit Hold",
            "color": 4,
        })
        wholesale = self.env["res.partner.category"].create({
            "name": "Wholesale",
            "color": 7,
        })
        customer = self.env["res.partner"].create({
            "name": "Dashboard Tagged AR Customer",
            "category_id": [Command.set((credit_hold | wholesale).ids)],
        })
        billing_contact = self.env["res.partner"].create({
            "name": "Dashboard Tagged Billing Contact",
            "parent_id": customer.id,
            "type": "invoice",
        })
        receivable = self._test_account(
            "TESTARTG", "asset_receivable", "Test Tagged AR Account",
            reconcile=True,
        )

        _move, current_line = self._post_partner_residual(
            today, 100.0, receivable, billing_contact, "Current tagged AR",
            due_date=today + timedelta(days=5),
        )
        _move, aged_line = self._post_partner_residual(
            today, 250.0, receivable, customer, "Aged tagged AR",
            due_date=today - timedelta(days=45),
        )
        _move, older_line = self._post_partner_residual(
            today, 75.0, receivable, customer, "Older tagged AR",
            due_date=today - timedelta(days=150),
        )
        self.env["account.move.line"].flush_model([
            "amount_residual", "reconciled", "account_id", "partner_id",
            "date_maturity",
        ])

        report = self.dashboard._ar_aging_report(company)
        row = next(
            item for item in report["rows"]
            if item["id"] == customer.commercial_partner_id.id
        )

        self.assertEqual(
            [bucket["label"] for bucket in report["buckets"]],
            ["Current", "1-30", "31-60", "61-90", "91-120", "Older"],
        )
        self.assertEqual(row["customer"], customer.display_name)
        self.assertEqual(row["tags_label"], "Credit Hold, Wholesale")
        self.assertEqual(
            row["tags"],
            [
                {"id": credit_hold.id, "name": "Credit Hold", "color": 4},
                {"id": wholesale.id, "name": "Wholesale", "color": 7},
            ],
        )
        self.assertEqual(row["line_count"], 3)
        self.assertAlmostEqual(row["current"], 100.0, places=2)
        self.assertAlmostEqual(row["days_31_60"], 250.0, places=2)
        self.assertAlmostEqual(row["older"], 75.0, places=2)
        self.assertAlmostEqual(row["total"], 425.0, places=2)
        drilldown_ids = row["drilldown"]["domain"][0][2]
        self.assertEqual(
            set(drilldown_ids),
            set((current_line | aged_line | older_line).ids),
        )

        customer.category_id = [Command.set(wholesale.ids)]
        refreshed_row = next(
            item for item in self.dashboard._ar_aging_report(company)["rows"]
            if item["id"] == customer.commercial_partner_id.id
        )
        self.assertEqual(refreshed_row["tags_label"], "Wholesale")
        self.assertEqual(refreshed_row["tags"], [
            {"id": wholesale.id, "name": "Wholesale", "color": 7},
        ])

    def test_ar_offsets_and_ap_are_separate_signed_balances(self):
        company = self.env.company
        entry_date = self._last_complete_week_start()
        customer = self.env["res.partner"].create({
            "name": "Dashboard AR Offset Customer",
        })
        vendor = self.env["res.partner"].create({
            "name": "Dashboard AP Vendor",
        })
        receivable = self._test_account(
            "TESTAROF", "asset_receivable", "Test AR Offset Account",
            reconcile=True)
        payable = self._test_account(
            "TESTAPBL", "liability_payable", "Test AP Account",
            reconcile=True)
        receivable.reconcile = True
        payable.reconcile = True
        customer.property_account_receivable_id = receivable
        vendor.property_account_payable_id = payable

        before_offsets = self.dashboard._ar_offsets_payload(company)
        before_open_ar = self.dashboard._open_ar_totals(company)
        before_ap = self.dashboard._open_ap_totals(company)

        self._post_partner_residual(
            entry_date, 1000.0, receivable, customer,
            "Open customer receivable")
        stripe_move, stripe_line = self._post_partner_residual(
            entry_date, -123.45, receivable, customer,
            "Stripe processing fee offset")
        vendor_move, vendor_line = self._post_partner_residual(
            entry_date, -456.78, payable, vendor,
            "Open vendor payable")
        self.env["account.move.line"].flush_model([
            "amount_residual", "reconciled", "account_id", "partner_id",
        ])

        balances = self.dashboard._ar_customer_balances(company, limit=None)
        ar_offsets = self.dashboard._ar_offsets_payload(company, limit=None)
        ap_aging = self.dashboard._ap_aging_report(company)
        ap_lines = self.dashboard._ap_payable_lines(company, limit=None)
        open_ar = self.dashboard._open_ar_totals(company)
        open_ap = self.dashboard._open_ap_totals(company)
        payload = self.dashboard.retrieve_dashboard(
            date_start=fields.Date.to_string(entry_date),
            date_end=fields.Date.to_string(entry_date + timedelta(days=6)),
        )
        cards = {card["label"]: card for card in payload["summary_cards"]}

        customer_row = next(
            row for row in balances
            if row["id"] == customer.commercial_partner_id.id
        )
        offset_row = next(
            row for row in ar_offsets["rows"]
            if row["id"] == stripe_line.id
        )
        payable_row = next(
            row for row in ap_lines
            if row["id"] == vendor_line.id
        )
        payable_aging_row = next(
            row for row in ap_aging["rows"]
            if row["id"] == vendor.commercial_partner_id.id
        )

        self.assertAlmostEqual(customer_row["open_amount"], 1000.0, places=2)
        self.assertAlmostEqual(customer_row["offset_amount"], 123.45, places=2)
        self.assertAlmostEqual(customer_row["net_amount"], 876.55, places=2)
        self.assertEqual(customer_row["status"], "Open balance with offsets")
        self.assertEqual(
            offset_row["reason"], "Stripe settlement offset")
        self.assertEqual(offset_row["move_id"], stripe_move.id)
        self.assertAlmostEqual(
            open_ar["amount"] - before_open_ar["amount"], 1000.0, places=2)
        self.assertAlmostEqual(
            ar_offsets["total"] - before_offsets["total"], 123.45, places=2)
        self.assertAlmostEqual(
            payload["open_ar"]["amount"] - before_open_ar["amount"],
            1000.0, places=2)
        self.assertAlmostEqual(
            payload["open_ar"]["offset_amount"] - before_offsets["total"],
            123.45, places=2)
        self.assertAlmostEqual(
            payload["open_ar"]["net_amount"] - (
                before_open_ar["amount"] - before_offsets["total"]),
            876.55, places=2)
        self.assertAlmostEqual(
            open_ap["amount"] - before_ap["amount"], 456.78, places=2)
        self.assertEqual(payable_row["move_id"], vendor_move.id)
        self.assertEqual(payable_row["vendor"], vendor.display_name)
        self.assertAlmostEqual(payable_row["amount"], 456.78, places=2)
        self.assertEqual(
            [bucket["label"] for bucket in ap_aging["buckets"]],
            ["Current", "1-30", "31-60", "61-90", "91-120", "Older"],
        )
        self.assertAlmostEqual(payable_aging_row["total"], 456.78, places=2)
        self.assertEqual(payable_aging_row["line_count"], 1)
        self.assertIn("AR Credits / Offsets", cards)
        self.assertIn("Net AR", cards)
        self.assertIn("Open AP", cards)
        self.assertEqual(cards["Open AR"]["metric_key"], "open_ar")
        self.assertIn("ar_offsets", payload)
        self.assertIn("ap_aging_buckets", payload)
        self.assertIn("ap_aging_report", payload)
        self.assertIn("ap_payable_lines", payload)

    def test_cogs_matches_posted_cost_of_revenue_entries(self):
        week_start = self._last_complete_week_start()
        range_kwargs = {
            "date_start": fields.Date.to_string(week_start),
            "date_end": fields.Date.to_string(week_start + timedelta(days=6)),
        }
        cogs_account = self._account_by_type("expense_direct_cost", "TESTCOGS")
        root = self.env["product.category"].search(
            [("parent_id", "=", False)], limit=1)
        top = self.env["product.category"].create(
            {"name": "Test Materials", "parent_id": root.id})
        leaf = self.env["product.category"].create(
            {"name": "Test Hinges", "parent_id": top.id})
        product = self.env["product.product"].create(
            {"name": "Test Hinge", "categ_id": leaf.id})

        # Baseline: the staging test DB may already hold posted COGS entries
        # in this week, so every total below is asserted as a delta.
        before = self.dashboard.retrieve_dashboard(**range_kwargs)
        before_cogs = before["weekly"][0]["cogs"]
        before_other = {
            item["label"]: item["amount"]
            for item in before["cogs_by_category"]
        }.get("Other / manual entries", 0.0)
        before_account_total = sum(
            item["amount"] for item in before["cogs_by_account"])
        before_card = {
            card["label"]: card for card in before["summary_cards"]
        }["Posted COGS"]["value"]

        self._post_entry(week_start, 500.0, cogs_account, product=product)
        self._post_entry(week_start + timedelta(days=1), 200.0, cogs_account)
        self._post_entry(
            week_start + timedelta(days=2), 100.0, cogs_account, credit=True)

        payload = self.dashboard.retrieve_dashboard(**range_kwargs)
        row = payload["weekly"][0]

        # 500 (product, leaf category) + 200 (manual) - 100 (reversal) = 600
        self.assertAlmostEqual(row["cogs"] - before_cogs, 600.0, places=2)

        categories = {
            item["label"]: item for item in payload["cogs_by_category"]}
        # The category tree is created in-test, so its amount is entirely
        # ours; the leaf category rolls up to its top-level ancestor.
        self.assertAlmostEqual(
            categories["Test Materials"]["amount"], 500.0, places=2)
        # Product-less lines (manual 200 and reversal -100) net under Other.
        self.assertAlmostEqual(
            categories["Other / manual entries"]["amount"] - before_other,
            100.0, places=2)

        account_total = sum(
            item["amount"] for item in payload["cogs_by_account"])
        self.assertAlmostEqual(
            account_total - before_account_total, 600.0, places=2)
        category_total = sum(
            item["amount"] for item in payload["cogs_by_category"])
        # Same line set bucketed two ways; buckets are rounded independently,
        # so allow for cent-level drift.
        self.assertAlmostEqual(category_total, account_total, places=1)

        cards = {card["label"]: card for card in payload["summary_cards"]}
        self.assertIn("Posted COGS", cards)
        self.assertAlmostEqual(
            cards["Posted COGS"]["value"] - before_card, 600.0, places=2)
        self.assertEqual(cards["Posted COGS"]["metric_key"], "cogs")
        self.assertIn("cogs", payload["metric_definitions"])

    def test_cogs_excludes_draft_and_non_cogs_entries(self):
        week_start = self._last_complete_week_start()
        range_kwargs = {
            "date_start": fields.Date.to_string(week_start),
            "date_end": fields.Date.to_string(week_start + timedelta(days=6)),
        }
        cogs_account = self._account_by_type("expense_direct_cost", "TESTCOGS")
        expense_account = self._account_by_type("expense", "TESTEXP")

        before = self.dashboard.retrieve_dashboard(**range_kwargs)

        self._post_entry(week_start, 400.0, cogs_account, post=False)
        self._post_entry(week_start + timedelta(days=1), 300.0, expense_account)

        payload = self.dashboard.retrieve_dashboard(**range_kwargs)

        # Draft and non-Cost-of-Revenue entries must not move any COGS figure.
        self.assertAlmostEqual(
            payload["weekly"][0]["cogs"], before["weekly"][0]["cogs"],
            places=2)
        self.assertEqual(
            payload["cogs_by_category"], before["cogs_by_category"])
        self.assertEqual(
            payload["cogs_by_account"], before["cogs_by_account"])
