from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class UnicoFinancialDashboard(models.AbstractModel):
    _name = "unico.financial.dashboard"
    _description = "Unico Financial KPI Dashboard"

    LOW_MARGIN_THRESHOLD = 0.20
    # Reporting weeks run Wednesday through Tuesday (customer requirement).
    WEEK_ANCHOR_WEEKDAY = 2
    MAX_WEEKS = 53
    DEFAULT_COMPLETE_WEEKS = 8

    @api.model
    def retrieve_dashboard(self, date_start=None, date_end=None, company_id=None,
                           comparison_mode=None, comparison_date_start=None,
                           comparison_date_end=None, range_preset=None):
        """Return a compact week-over-week financial dashboard payload.

        ``date_start``/``date_end`` (YYYY-MM-DD strings) are kept as the
        selected range and split into Wednesday->Tuesday reporting buckets.
        When absent, invalid, or reversed the dashboard shows the last 8
        complete weeks plus the current in-progress week.

        All reads stay in Odoo ORM/search APIs so record rules, company access,
        and normal Odoo field conversions remain in force.
        """
        self._check_dashboard_access()
        company = self._get_dashboard_company(company_id)
        range_preset, windows = self._resolve_range_for_preset(
            range_preset, date_start, date_end)
        weeks = len(windows)
        weekly = self._empty_weekly_rows(windows)
        start_date = windows[0]["start"]
        end_date = windows[-1]["end"]
        comparison_mode, prior_windows = self._comparison_week_windows(
            windows, comparison_mode, comparison_date_start,
            comparison_date_end)
        prior_weekly = self._empty_weekly_rows(prior_windows)

        current_week = windows[-1]
        prior_week = windows[-2] if len(windows) > 1 else windows[-1]

        range_meta = self._fill_weekly_rows(weekly, windows, company)
        self._fill_weekly_rows(prior_weekly, prior_windows, company)

        current_domains = self._current_week_domains(current_week, company)
        range_domains = self._range_domains(windows[0]["start"], windows[-1]["end"], company)
        current_open_domains = self._current_open_domains(company)
        generated_at = fields.Datetime.to_string(fields.Datetime.now())
        summary_context = self._summary_context(
            windows, prior_windows, generated_at, comparison_mode)
        pre_invoice_drilldown = {
            "model": "account.payment",
            "domain": [("id", "in", range_meta["pre_invoice_payment_ids"])],
        }
        cogs_by_category, cogs_by_account = self._cogs_breakdown_payload(
            range_meta["cogs"], windows[0]["start"], windows[-1]["end"], company)
        inventory_value = self._inventory_value_payload(company)
        inventory_value["history"] = {"points": [], "series": [], "deferred": True}
        open_ar = self._open_ar_totals(company)
        ar_offsets = self._ar_offsets_payload(company)
        open_ar["offset_amount"] = ar_offsets["total"]
        open_ar["net_amount"] = self._round(
            open_ar["amount"] - ar_offsets["total"])
        open_ap = self._open_ap_totals(company)
        open_delivery = self._open_delivery_totals(company)
        range_summary = self._range_summary(weekly)
        prior_range_summary = self._range_summary(prior_weekly)

        return {
            "generated_at": generated_at,
            "company": {"id": company.id, "name": company.display_name},
            "currency": self._currency_payload(company.currency_id),
            "date_start": fields.Date.to_string(windows[0]["start"]),
            "date_end": fields.Date.to_string(windows[-1]["end"] - timedelta(days=1)),
            "range_preset": range_preset,
            "comparison_mode": comparison_mode,
            "comparison_date_start": fields.Date.to_string(prior_windows[0]["start"]),
            "comparison_date_end": fields.Date.to_string(
                prior_windows[-1]["end"] - timedelta(days=1)),
            "weeks": [
                {
                    "key": row["key"],
                    "label": row["label"],
                    "range_label": row["range_label"],
                    "start": fields.Date.to_string(row["start"]),
                    "end": fields.Date.to_string(row["end"] - timedelta(days=1)),
                    "partial": row["partial"],
                }
                for row in weekly
            ],
            "summary_context": summary_context,
            "metric_definitions": self._metric_definitions(),
            "summary_cards": self._summary_cards(
                range_summary, prior_range_summary, range_domains,
                current_open_domains, open_ar, ar_offsets, open_ap,
                open_delivery, inventory_value, weeks,
                pre_invoice_drilldown, summary_context,
            ),
            "weekly": weekly,
            "cogs_by_category": cogs_by_category,
            "cogs_by_account": cogs_by_account,
            "inventory_value": inventory_value,
            "open_sales_order_kpis": self._open_sales_order_kpis(company),
            "aging_buckets": self._aging_buckets(company),
            "ar_aging_report": self._ar_aging_report(company),
            "open_ar": open_ar,
            "ar_customer_balances": self._ar_customer_balances(company),
            "ar_offsets": ar_offsets,
            "ap_aging_buckets": self._ap_aging_buckets(company),
            "ap_aging_report": self._ap_aging_report(company),
            "ap_payable_lines": self._ap_payable_lines(company),
            "open_ap": open_ap,
            "open_delivery": open_delivery,
            "fulfillment_conversion": self._fulfillment_conversion_rows(
                start_date,
                end_date,
                company,
                comparison_start=prior_windows[0]["start"],
                comparison_end=prior_windows[-1]["end"],
            ),
            "salespeople": self._salesperson_rows(start_date, end_date, company),
            "payment_term_analysis": self._payment_term_rows(
                start_date,
                end_date,
                company,
                comparison_start=prior_windows[0]["start"],
                comparison_end=prior_windows[-1]["end"],
            ),
            "low_margin_orders": self._low_margin_orders(start_date, end_date, company),
            "rma_exposure": self._rma_exposure_rows(company),
            "stripe_status": self._stripe_status_rows(start_date, end_date, company),
            "payment_exceptions": self._payment_exception_rows(company),
            "unpaid_delivery_orders": self._unpaid_delivery_order_rows(company),
            "drilldowns": {
                "current_week": current_domains,
                "current_open": current_open_domains,
                "prior_week": self._current_week_domains(prior_week, company),
            },
        }

    @api.model
    def retrieve_inventory_history(self, date_start=None, date_end=None,
                                   company_id=None, range_preset=None):
        """Return period-end inventory valuation history for the selected range.

        This is intentionally separate from ``retrieve_dashboard`` because
        historical stock valuation can be expensive. The main dashboard should
        render first, then load this section on demand.
        """
        self._check_dashboard_access()
        company = self._get_dashboard_company(company_id)
        _range_preset, windows = self._resolve_range_for_preset(
            range_preset, date_start, date_end)
        return {
            "history": self._inventory_history_payload(windows, company),
        }

    @api.model
    def _check_dashboard_access(self):
        if (
            self.env.user.has_group("account.group_account_readonly")
            or self.env.user.has_group("account.group_account_invoice")
            or self.env.user.has_group("account.group_account_manager")
        ):
            return
        raise AccessError(_("Only accounting users can open the financial KPI dashboard."))

    @api.model
    def _get_dashboard_company(self, company_id=None):
        if company_id:
            company = self.env["res.company"].browse(int(company_id)).exists()
            if not company or company not in self.env.companies:
                raise AccessError(_("You do not have access to this company."))
            return company
        return self.env.company

    @api.model
    def _week_start(self, value_date):
        """Snap a date back to its reporting week's start (Wednesday)."""
        return value_date - timedelta(
            days=(value_date.weekday() - self.WEEK_ANCHOR_WEEKDAY) % 7
        )

    @api.model
    def _parse_date(self, value):
        if not value:
            return None
        try:
            return fields.Date.to_date(value)
        except (TypeError, ValueError):
            return None

    @api.model
    def _default_windows(self):
        today = fields.Date.context_today(self)
        first_start = self._week_start(today) - timedelta(
            weeks=self.DEFAULT_COMPLETE_WEEKS
        )
        return self._week_windows_from_start(
            first_start, self.DEFAULT_COMPLETE_WEEKS + 1
        )

    @api.model
    def _resolve_range_for_preset(self, range_preset=None, date_start=None,
                                  date_end=None):
        preset = range_preset or ("custom" if date_start and date_end else "default")
        if preset == "default":
            return "default", self._default_windows()
        if preset == "custom":
            return "custom", self._date_windows_from_range(date_start, date_end)

        today = fields.Date.context_today(self)
        preset_ranges = {
            "past_day": (today, today),
            "past_week": (today - timedelta(days=6), today),
            "past_month": (today - timedelta(days=29), today),
            "year_to_date": (today.replace(month=1, day=1), today),
            "past_year": (today - timedelta(days=364), today),
        }
        if preset in preset_ranges:
            start, end = preset_ranges[preset]
            return preset, self._date_windows_from_range(start, end)
        return "default", self._default_windows()

    @api.model
    def _resolve_range(self, date_start, date_end):
        start = self._parse_date(date_start)
        end = self._parse_date(date_end)
        if not start or not end or start > end:
            return self._default_windows()
        first_start = self._week_start(start)
        weeks = (self._week_start(end) - first_start).days // 7 + 1
        if weeks > self.MAX_WEEKS:
            weeks = self.MAX_WEEKS
            first_start = self._week_start(end) - timedelta(weeks=weeks - 1)
        return self._week_windows_from_start(first_start, weeks)

    @api.model
    def _date_windows_from_range(self, start, end):
        start = self._parse_date(start)
        end = self._parse_date(end)
        if not start or not end or start > end:
            return self._default_windows()

        first_week_start = self._week_start(start)
        last_week_start = self._week_start(end)
        weeks = (last_week_start - first_week_start).days // 7 + 1
        if weeks > self.MAX_WEEKS:
            weeks = self.MAX_WEEKS
            first_week_start = last_week_start - timedelta(weeks=weeks - 1)
            start = max(start, first_week_start)

        today = fields.Date.context_today(self)
        end_exclusive = end + timedelta(days=1)
        windows = []
        for index in range(weeks):
            reporting_start = first_week_start + timedelta(weeks=index)
            reporting_end = reporting_start + timedelta(days=7)
            window_start = max(start, reporting_start)
            window_end = min(end_exclusive, reporting_end)
            if window_start >= window_end:
                continue
            end_inclusive = window_end - timedelta(days=1)
            range_label = self._date_range_label(window_start, end_inclusive)
            windows.append({
                "key": fields.Date.to_string(window_start),
                "label": "%s %s" % (window_start.strftime("%b"), window_start.day),
                "range_label": range_label,
                "start": window_start,
                "end": window_end,
                "partial": end_inclusive >= today and window_end < reporting_end,
            })
        return windows or self._default_windows()

    @api.model
    def _prior_week_windows(self, first_start, weeks):
        return self._week_windows_from_start(first_start - timedelta(weeks=weeks), weeks)

    @api.model
    def _prior_period_windows(self, windows):
        start = windows[0]["start"]
        end = windows[-1]["end"] - timedelta(days=1)
        days = (end - start).days + 1
        prior_end = start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=days - 1)
        return self._date_windows_from_range(prior_start, prior_end)

    @api.model
    def _comparison_week_windows(self, windows, comparison_mode=None,
                                 comparison_date_start=None,
                                 comparison_date_end=None):
        mode = comparison_mode or "prior_period"
        if mode == "same_period_last_year":
            return mode, self._date_windows_from_range(
                windows[0]["start"] - timedelta(weeks=52),
                windows[-1]["end"] - timedelta(weeks=52, days=1))
        if mode == "custom":
            start = self._parse_date(comparison_date_start)
            end = self._parse_date(comparison_date_end)
            if start and end and start <= end:
                return mode, self._date_windows_from_range(start, end)
        return "prior_period", self._prior_period_windows(windows)

    @api.model
    def _week_windows_from_start(self, first_start, weeks):
        today = fields.Date.context_today(self)
        windows = []
        for index in range(weeks):
            start = first_start + timedelta(weeks=index)
            end = start + timedelta(days=7)
            end_inclusive = end - timedelta(days=1)
            windows.append({
                "key": fields.Date.to_string(start),
                "label": "%s %s" % (start.strftime("%b"), start.day),
                "range_label": "%s %s-%s %s" % (
                    start.strftime("%b"),
                    start.day,
                    end_inclusive.strftime("%b"),
                    end_inclusive.day,
                ),
                "start": start,
                "end": end,
                "partial": end > today,
            })
        return windows

    @api.model
    def _empty_weekly_rows(self, windows):
        return [
            {
                "key": window["key"],
                "label": window["label"],
                "range_label": window["range_label"],
                "start": window["start"],
                "end": window["end"],
                "partial": window.get("partial", False),
                "revenue": 0.0,
                "gross_profit": 0.0,
                "margin_percent": 0.0,
                "cogs": 0.0,
                "credit_notes": 0.0,
                "refunds": 0.0,
                "unpaid_invoices": 0.0,
                "unpaid_invoice_count": 0,
                "cash_collected": 0.0,
                "collected_pre_invoice": 0.0,
                "collected_invoiced": 0.0,
                "payment_exceptions": 0,
                "confirmed_sales": 0.0,
                "sales": 0.0,
                "sales_order_count": 0,
                "low_margin_order_count": 0,
                "rma_credit_exposure": 0.0,
                "posted_credit_notes": 0.0,
                "replacement_value": 0.0,
                "unpaid_delivery_orders": 0.0,
                "unpaid_delivery_count": 0,
                "stripe_succeeded": 0,
                "stripe_exceptions": 0,
                "reconciliation_exceptions": 0,
            }
            for window in windows
        ]

    @api.model
    def _fill_weekly_rows(self, weekly, windows, company):
        self._fill_financial_summary(weekly, windows, company)
        self._fill_sales_quality(weekly, windows, company)
        self._fill_returns_credits(weekly, windows, company)
        self._fill_cash_ar(weekly, windows, company)
        self._fill_operational_finance(weekly, windows, company)
        meta = self._fill_cash_collected(weekly, windows, company)
        meta["cogs"] = self._fill_cogs(weekly, windows, company)
        return meta

    @api.model
    def _range_summary(self, weekly):
        summary = dict(self._empty_weekly_rows([{
            "key": "range",
            "label": _("Selected Range"),
            "range_label": "",
            "start": weekly[0]["start"] if weekly else fields.Date.context_today(self),
            "end": weekly[-1]["end"] if weekly else fields.Date.context_today(self),
        }])[0])
        for row in weekly:
            for key, value in row.items():
                if key in ("key", "label", "range_label", "start", "end",
                           "margin_percent", "partial"):
                    continue
                if isinstance(value, (int, float)):
                    summary[key] += value
        summary["margin_percent"] = self._ratio_percent(summary["gross_profit"], summary["sales"])
        for key, value in list(summary.items()):
            if isinstance(value, float):
                summary[key] = self._round(value)
        return summary

    @api.model
    def _range_label(self, windows):
        if not windows:
            return ""
        label = self._date_range_label(
            windows[0]["start"], windows[-1]["end"] - timedelta(days=1))
        if windows[-1].get("partial"):
            label = _("%s (includes current partial week)") % label
        return label

    @api.model
    def _short_date_label(self, value_date):
        return "%s %s" % (value_date.strftime("%b"), value_date.day)

    @api.model
    def _long_date_label(self, value_date):
        return "%s %s, %s" % (
            value_date.strftime("%b"), value_date.day, value_date.year)

    @api.model
    def _date_range_label(self, start, end, long=False):
        if long and start.year == end.year:
            return _("%(start)s - %(end)s, %(year)s") % {
                "start": "%s %s" % (start.strftime("%B"), start.day),
                "end": "%s %s" % (end.strftime("%B"), end.day),
                "year": end.year,
            }
        formatter = self._long_date_label if long else self._short_date_label
        return _("%(start)s - %(end)s") % {
            "start": formatter(start),
            "end": formatter(end),
        }

    @api.model
    def _week_count_phrase(self, weeks):
        return _("1 week") if weeks == 1 else _("%s weeks") % weeks

    @api.model
    def _prior_range_label(self, weeks):
        return _("vs prior %s") % self._week_count_phrase(weeks)

    @api.model
    def _summary_context(self, windows, prior_windows, generated_at=None,
                         comparison_mode=None):
        today = fields.Date.context_today(self)
        start = windows[0]["start"]
        end = windows[-1]["end"] - timedelta(days=1)
        prior_start = prior_windows[0]["start"]
        prior_end = prior_windows[-1]["end"] - timedelta(days=1)
        weeks = len(windows)
        current_week = windows[-1]
        current_week_end = current_week["end"] - timedelta(days=1)
        partial = bool(current_week.get("partial"))
        partial_label = ""
        if partial:
            partial_label = _("%(week)s is partial through %(today)s") % {
                "week": current_week["range_label"],
                "today": self._long_date_label(today),
            }
        return {
            "range_label": self._range_label(windows),
            "range_dates": self._date_range_label(start, end, long=True),
            "range_dates_short": self._date_range_label(start, end),
            "comparison_mode": comparison_mode or "prior_period",
            "comparison_label": self._comparison_label(
                comparison_mode or "prior_period", weeks),
            "comparison_date_start": fields.Date.to_string(prior_start),
            "comparison_date_end": fields.Date.to_string(prior_end),
            "comparison_dates": self._date_range_label(
                prior_start, prior_end, long=True),
            "comparison_dates_short": self._date_range_label(
                prior_start, prior_end),
            "week_count_label": self._week_count_phrase(weeks),
            "reporting_week_label": _("Reporting weeks run Wednesday-Tuesday"),
            "partial": partial,
            "partial_label": partial_label,
            "current_week_label": self._date_range_label(
                current_week["start"], current_week_end),
            "snapshot_label": _("As of %s") % self._long_date_label(today),
            "generated_label": _("Generated %s") % (
                generated_at or fields.Datetime.to_string(fields.Datetime.now())),
        }

    @api.model
    def _comparison_label(self, comparison_mode, weeks):
        if comparison_mode == "same_period_last_year":
            return _("vs same period last year")
        if comparison_mode == "custom":
            return _("vs custom comparison")
        return self._prior_range_label(weeks)

    @api.model
    def _metric_definitions(self):
        return {
            "key": "Wednesday-Tuesday reporting period.",
            "revenue": "Posted customer invoice revenue for the week, before "
                       "tax. This does not include customer deposits collected "
                       "before invoicing.",
            "confirmed_sales": "Confirmed sales order value for the week, "
                               "before tax. This is order volume, not cash "
                               "collected.",
            "gross_profit": "Estimated gross profit from confirmed sales "
                            "orders.",
            "margin_percent": "Estimated gross profit divided by booked sales.",
            "cogs": "Posted Cost of Goods Sold / Cost of Revenue activity for "
                    "the week.",
            "inventory_value": "Current value of inventory on hand, grouped "
                               "by the inventory account that owns the value.",
            "credit_notes": "Posted customer credits for the week.",
            "refunds": "Refund payments issued to customers during the week.",
            "open_ar": "Customer receivable balances still open today.",
            "unpaid_invoices": "Currently unpaid invoice balances grouped by "
                               "invoice week.",
            "ar_offsets": "Credits, unapplied payments, Stripe fees, or other "
                          "receivable offsets. These reduce net AR but are not "
                          "money owed to customers.",
            "net_ar": "Open customer invoice balances minus AR credits and offsets.",
            "open_ap": "Vendor bills and payable balances still unpaid.",
            "cash_collected": "Customer payments received in this period, "
                              "including terminal, online, cash, and manual "
                              "payments.",
            "collected_pre_invoice": "Customer payments received before "
                                     "invoicing.",
            "collected_invoiced": "Customer payments received after invoicing.",
            "rma_credit_exposure": "Estimated credit exposure from open RMAs.",
            "replacement_value": "Replacement product value created from RMAs "
                                 "in this week.",
            "unpaid_delivery_orders": "Confirmed delivery orders still marked "
                                      "unpaid.",
            "payment_exceptions": "Items needing accounting/payment review.",
            "stripe_exceptions": "Stripe terminal payments that failed, need "
                                 "action, or are still processing.",
        }

    @api.model
    def _current_week_domains(self, window, company):
        return self._range_domains(window["start"], window["end"], company)

    @api.model
    def _range_domains(self, start, end, company):
        date_start = fields.Date.to_string(start)
        date_end = fields.Date.to_string(end)
        dt_start = fields.Datetime.to_string(datetime.combine(start, time.min))
        dt_end = fields.Datetime.to_string(datetime.combine(end, time.min))
        company_domain = [("company_id", "=", company.id)]
        sales_domain = company_domain + [
            ("state", "in", ("sale", "done")),
            ("date_order", ">=", dt_start),
            ("date_order", "<", dt_end),
        ]
        return {
            "revenue": {
                "model": "account.move",
                "domain": company_domain + [
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                    ("invoice_date", ">=", date_start),
                    ("invoice_date", "<", date_end),
                ],
            },
            "credit_notes": {
                "model": "account.move",
                "domain": company_domain + [
                    ("move_type", "=", "out_refund"),
                    ("state", "=", "posted"),
                    ("invoice_date", ">=", date_start),
                    ("invoice_date", "<", date_end),
                ],
            },
            "refunds": {
                "model": "account.payment",
                "domain": company_domain + [
                    ("partner_type", "=", "customer"),
                    ("payment_type", "=", "outbound"),
                    ("state", "in", ("paid", "in_process")),
                    ("date", ">=", date_start),
                    ("date", "<", date_end),
                ],
            },
            "cash_collected": {
                "model": "account.payment",
                "domain": self._collected_payment_domain(start, end, company),
            },
            "cogs": {
                "model": "account.move.line",
                "domain": self._cogs_line_domain(start, end, company),
            },
            "confirmed_sales": {
                "model": "sale.order",
                "domain": sales_domain,
            },
            "sales": {
                "model": "sale.order",
                "domain": sales_domain,
            },
            "rmas": {
                "model": "rma.main",
                "domain": company_domain + [
                    ("date", ">=", dt_start),
                    ("date", "<", dt_end),
                ],
            },
            "rma_credit_exposure": {
                "model": "rma.main",
                "domain": company_domain + [
                    ("state", "in", ("draft", "submit", "approved", "processing")),
                    ("date", ">=", dt_start),
                    ("date", "<", dt_end),
                ],
            },
            "stripe_payments": {
                "model": "stripe.terminal.payment",
                "domain": company_domain + [
                    ("create_date", ">=", dt_start),
                    ("create_date", "<", dt_end),
                ],
            },
            "stripe_exceptions": {
                "model": "stripe.terminal.payment",
                "domain": company_domain + [
                    ("status", "in", ("failed", "requires_action", "processing")),
                    ("create_date", ">=", dt_start),
                    ("create_date", "<", dt_end),
                ],
            },
        }

    @api.model
    def _current_open_domains(self, company):
        today = fields.Date.context_today(self)
        today_str = fields.Date.to_string(today)
        company_domain = [("company_id", "=", company.id)]
        unpaid_invoice_domain = company_domain + [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
            ("amount_residual", ">", 0),
        ]
        return {
            "unpaid_invoices": {"model": "account.move", "domain": unpaid_invoice_domain},
            "positive_receivable_lines": {
                "model": "account.move.line",
                "domain": self._receivable_line_domain(company, sign="positive"),
            },
            "negative_receivable_lines": {
                "model": "account.move.line",
                "domain": self._receivable_line_domain(company, sign="negative"),
            },
            "open_payable_lines": {
                "model": "account.move.line",
                "domain": self._payable_line_domain(company, sign="open"),
            },
            "overdue_invoices": {
                "model": "account.move",
                "domain": unpaid_invoice_domain + [("invoice_date_due", "<", today_str)],
            },
            "unpaid_delivery_orders": {
                "model": "sale.order",
                "domain": company_domain + [
                    ("state", "=", "sale"),
                    ("fulfillment_type", "=", "delivery"),
                    ("unico_payment_status", "=", "not_paid"),
                ],
            },
            "rma_credit_exposure": {
                "model": "rma.main",
                "domain": company_domain + [
                    ("state", "in", ("draft", "submit", "approved", "processing")),
                ],
            },
            "stripe_exceptions": {
                "model": "stripe.terminal.payment",
                "domain": company_domain + [
                    ("status", "in", ("failed", "requires_action", "processing")),
                ],
            },
        }

    @api.model
    def _open_sales_order_kpis(self, company):
        today = fields.Date.context_today(self)
        start = today - timedelta(days=29)
        end = today + timedelta(days=1)
        dt_start = fields.Datetime.to_string(datetime.combine(start, time.min))
        dt_end = fields.Datetime.to_string(datetime.combine(end, time.min))
        company_domain = [("company_id", "=", company.id)]
        window_domain = [
            ("date_order", ">=", dt_start),
            ("date_order", "<", dt_end),
        ]
        confirmed_domain = company_domain + [
            ("state", "in", ("sale", "done")),
        ] + window_domain
        open_domain = company_domain + [
            ("state", "=", "sale"),
            ("invoice_status", "!=", "invoiced"),
            ("unico_payment_status", "=", "not_paid"),
        ] + window_domain
        will_call_domain = open_domain + [("fulfillment_type", "=", "will_call")]
        delivery_domain = open_domain + [("fulfillment_type", "=", "delivery")]

        confirmed_orders = self.env["sale.order"].search(confirmed_domain)
        open_orders = self.env["sale.order"].search(open_domain)
        will_call_orders = self.env["sale.order"].search(will_call_domain)
        delivery_orders = self.env["sale.order"].search(delivery_domain)

        confirmed_total = self._sales_order_total(confirmed_orders, company)
        open_total = self._sales_order_total(open_orders, company)
        will_call_total = self._sales_order_total(will_call_orders, company)
        delivery_total = self._sales_order_total(delivery_orders, company)
        average = confirmed_total / len(confirmed_orders) if confirmed_orders else 0.0

        def drilldown(domain):
            return {"model": "sale.order", "domain": domain}

        def card(label, value, value_type, domain, definition):
            return {
                "label": label,
                "value": self._round(value),
                "value_type": value_type,
                "drilldown": drilldown(domain),
                "definition": definition,
            }

        return {
            "date_start": fields.Date.to_string(start),
            "date_end": fields.Date.to_string(today),
            "window_label": self._date_range_label(start, end - timedelta(days=1)),
            "cards": [
                card(
                    _("Booked Sales #"), len(confirmed_orders), "number",
                    confirmed_domain,
                    _("Confirmed sales orders dated in the last 30 days."),
                ),
                card(
                    _("Booked Sales $"), confirmed_total, "money",
                    confirmed_domain,
                    _("Total order value for confirmed sales orders dated in the last 30 days."),
                ),
                card(
                    _("Average Order $"), average, "money", confirmed_domain,
                    _("Average order value for confirmed sales orders dated in the last 30 days."),
                ),
                card(
                    _("Open Sales Orders #"), len(open_orders), "number",
                    open_domain,
                    _("Confirmed sales orders dated in the last 30 days that are not fully invoiced and are still unpaid."),
                ),
                card(
                    _("Open Sales Orders $"), open_total, "money",
                    open_domain,
                    _("Total value of confirmed sales orders dated in the last 30 days that are not fully invoiced and are still unpaid."),
                ),
                card(
                    _("Open Will Call #"), len(will_call_orders), "number",
                    will_call_domain,
                    _("Unpaid, not fully invoiced will call/customer pickup sales orders dated in the last 30 days."),
                ),
                card(
                    _("Open Will Call $"), will_call_total, "money",
                    will_call_domain,
                    _("Total value of unpaid, not fully invoiced will call/customer pickup sales orders dated in the last 30 days."),
                ),
                card(
                    _("Open Deliveries #"), len(delivery_orders), "number",
                    delivery_domain,
                    _("Unpaid, not fully invoiced delivery sales orders dated in the last 30 days."),
                ),
                card(
                    _("Open Deliveries $"), delivery_total, "money",
                    delivery_domain,
                    _("Total value of unpaid, not fully invoiced delivery sales orders dated in the last 30 days."),
                ),
            ],
        }

    @api.model
    def _sales_order_total(self, orders, company):
        return self._round(sum(
            self._to_company_currency(
                order.amount_total,
                order.currency_id,
                fields.Date.to_date(order.date_order),
                company,
            )
            for order in orders
        ))

    @api.model
    def _summary_cards(self, current, prior, range_domains, current_open_domains,
                       open_ar, ar_offsets, open_ap, open_delivery,
                       inventory_value, weeks,
                       pre_invoice_drilldown=None, summary_context=None):
        summary_context = summary_context or {}
        period_context = _("Period total")
        snapshot_context = _("Current snapshot")
        period_label = summary_context.get("range_dates_short") or _("Selected range")
        snapshot_label = summary_context.get("snapshot_label") or _("Current snapshot")
        comparison_label = (
            summary_context.get("comparison_dates_short")
            or self._prior_range_label(weeks).replace("vs ", "")
        )
        prior_range = _("vs %s") % comparison_label
        ar_offset_total = ar_offsets.get("total", 0.0)
        net_ar = open_ar.get(
            "net_amount", open_ar.get("amount", 0.0) - ar_offset_total)
        cards = [
            self._card("Invoiced Revenue", current["revenue"], prior["revenue"], "money", "good_up", range_domains["revenue"], period_context, prior_range, metric_key="revenue", period_label=period_label),
            self._card("Booked Sales", current["confirmed_sales"], prior["confirmed_sales"], "money", "good_up", range_domains["confirmed_sales"], period_context, prior_range, metric_key="confirmed_sales", period_label=period_label),
            self._card("Estimated Gross Profit", current["gross_profit"], prior["gross_profit"], "money", "good_up", range_domains["sales"], period_context, prior_range, metric_key="gross_profit", period_label=period_label),
            self._card("Estimated Margin %", current["margin_percent"], prior["margin_percent"], "percent", "good_up", range_domains["sales"], period_context, prior_range, metric_key="margin_percent", period_label=period_label),
            self._card("Posted COGS", current["cogs"], prior["cogs"], "money", "good_down", range_domains.get("cogs"), period_context, prior_range, metric_key="cogs", period_label=period_label),
            self._card("Credit Memos", current["credit_notes"], prior["credit_notes"], "money", "good_down", range_domains["credit_notes"], period_context, prior_range, metric_key="credit_notes", period_label=period_label),
            self._card("Customer Refunds", current["refunds"], prior["refunds"], "money", "good_down", range_domains["refunds"], period_context, prior_range, metric_key="refunds", period_label=period_label),
            self._card("Open AR", open_ar["amount"], 0.0, "money", "good_down", current_open_domains["positive_receivable_lines"], snapshot_context, "", False, metric_key="open_ar", period_label=snapshot_label),
            self._card("AR Credits / Offsets", ar_offset_total, 0.0, "money", "good_down", current_open_domains["negative_receivable_lines"], snapshot_context, "", False, metric_key="ar_offsets", period_label=snapshot_label),
            self._card("Net AR", net_ar, 0.0, "money", "good_down", {}, snapshot_context, "", False, metric_key="net_ar", period_label=snapshot_label),
            self._card("Open AP", open_ap["amount"], 0.0, "money", "good_down", current_open_domains["open_payable_lines"], snapshot_context, "", False, metric_key="open_ap", period_label=snapshot_label),
            self._card("On-Hand Inventory", inventory_value["total"], 0.0, "money", "good_up", {}, snapshot_context, "", False, metric_key="inventory_value", period_label=snapshot_label),
            self._card("Cash Collected", current["cash_collected"], prior["cash_collected"], "money", "good_up", range_domains.get("cash_collected"), period_context, prior_range, metric_key="cash_collected", period_label=period_label),
            self._card("Pre-Invoice Collections", current["collected_pre_invoice"], prior["collected_pre_invoice"], "money", "good_up", pre_invoice_drilldown, period_context, prior_range, metric_key="collected_pre_invoice", period_label=period_label),
            self._card("Open RMA Exposure", current["rma_credit_exposure"], prior["rma_credit_exposure"], "money", "good_down", range_domains["rma_credit_exposure"], period_context, prior_range, metric_key="rma_credit_exposure", period_label=period_label),
            self._card("Unpaid Delivery Orders", open_delivery["amount"], 0.0, "money", "good_down", current_open_domains["unpaid_delivery_orders"], snapshot_context, "", False, metric_key="unpaid_delivery_orders", period_label=snapshot_label),
            self._card("Stripe Exceptions", current["stripe_exceptions"], prior["stripe_exceptions"], "number", "good_down", range_domains["stripe_exceptions"], period_context, prior_range, metric_key="stripe_exceptions", period_label=period_label),
        ]
        return cards

    @api.model
    def _card(self, label, value, prior, value_type, direction, drilldown,
              context_label=None, delta_label=None, show_delta=True,
              metric_key=None, period_label=None):
        value = self._round(value)
        prior = self._round(prior)
        delta = self._round(value - prior)
        if value_type == "percent":
            delta = self._round(value - prior, 2)
        return {
            "label": label,
            "value": value,
            "prior_value": prior,
            "delta": delta,
            "delta_percent": self._percent_change(value, prior),
            "value_type": value_type,
            "direction": direction,
            "drilldown": drilldown or {},
            "context_label": context_label or "",
            "period_label": period_label or "",
            "delta_label": delta_label if delta_label is not None else _("WoW"),
            "show_delta": show_delta,
            "metric_key": metric_key or "",
        }

    @api.model
    def _fill_financial_summary(self, weekly, windows, company):
        start = windows[0]["start"]
        end = windows[-1]["end"]
        move_domain = [
            ("company_id", "=", company.id),
            ("state", "=", "posted"),
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("invoice_date", ">=", fields.Date.to_string(start)),
            ("invoice_date", "<", fields.Date.to_string(end)),
        ]
        for move in self.env["account.move"].search(move_domain):
            row = self._row_for_date(weekly, windows, move.invoice_date)
            if not row:
                continue
            amount = abs(move.amount_untaxed_signed)
            if move.move_type == "out_invoice":
                row["revenue"] += amount
            elif move.move_type == "out_refund":
                row["credit_notes"] += amount
                row["posted_credit_notes"] += amount

        payment_domain = [
            ("company_id", "=", company.id),
            ("partner_type", "=", "customer"),
            ("payment_type", "=", "outbound"),
            ("state", "in", ("paid", "in_process")),
            ("date", ">=", fields.Date.to_string(start)),
            ("date", "<", fields.Date.to_string(end)),
        ]
        for payment in self.env["account.payment"].search(payment_domain):
            row = self._row_for_date(weekly, windows, payment.date)
            if row:
                row["refunds"] += abs(self._to_company_currency(
                    payment.amount, payment.currency_id, payment.date, company
                ))

    @api.model
    def _fill_sales_quality(self, weekly, windows, company):
        orders = self._sale_orders_for_range(windows[0]["start"], windows[-1]["end"], company)
        for order in orders:
            row = self._row_for_date(weekly, windows, fields.Date.to_date(order.date_order))
            if not row:
                continue
            order_date = fields.Date.to_date(order.date_order)
            sales = self._to_company_currency(order.amount_untaxed, order.currency_id, order_date, company)
            margin = self._to_company_currency(order.margin, order.currency_id, order_date, company)
            row["confirmed_sales"] += sales
            row["sales"] += sales
            row["gross_profit"] += margin
            row["sales_order_count"] += 1
            if order.amount_untaxed and order.margin_percent < self.LOW_MARGIN_THRESHOLD:
                row["low_margin_order_count"] += 1
        for row in weekly:
            row["margin_percent"] = self._ratio_percent(row["gross_profit"], row["sales"])

    @api.model
    def _fill_returns_credits(self, weekly, windows, company):
        rma_domain = [
            ("company_id", "=", company.id),
            ("date", ">=", fields.Datetime.to_string(datetime.combine(windows[0]["start"], time.min))),
            ("date", "<", fields.Datetime.to_string(datetime.combine(windows[-1]["end"], time.min))),
        ]
        for rma in self.env["rma.main"].search(rma_domain):
            row = self._row_for_date(weekly, windows, fields.Date.to_date(rma.date))
            if not row:
                continue
            if rma.state in ("draft", "submit", "approved", "processing"):
                row["rma_credit_exposure"] += rma.total_return
            row["replacement_value"] += rma.total_replace

    @api.model
    def _fill_cash_ar(self, weekly, windows, company):
        unpaid_domain = self._current_open_domains(company)["unpaid_invoices"]["domain"]
        unpaid_moves = self.env["account.move"].search(unpaid_domain)
        for move in unpaid_moves:
            row = self._row_for_date(
                weekly, windows, move.invoice_date or move.invoice_date_due
            )
            if row:
                row["unpaid_invoices"] += abs(move.amount_residual_signed)
                row["unpaid_invoice_count"] += 1

        exceptions = self._payment_exception_moves(company)
        for move in exceptions:
            row = self._row_for_date(
                weekly, windows, move.invoice_date_due or move.invoice_date
            )
            if row:
                row["payment_exceptions"] += 1
                row["reconciliation_exceptions"] += 1

    @api.model
    def _collected_payment_domain(self, start, end, company):
        return [
            ("company_id", "=", company.id),
            ("partner_type", "=", "customer"),
            ("payment_type", "=", "inbound"),
            ("state", "in", ("in_process", "paid")),
            ("date", ">=", fields.Date.to_string(start)),
            ("date", "<", fields.Date.to_string(end)),
        ]

    @api.model
    def _fill_cash_collected(self, weekly, windows, company):
        """Bucket inbound customer payments by payment date, split into
        pre-invoice collections (no posted customer invoice dated on/before
        the payment date existed for the payment's order) and invoiced
        collections. The classification compares dates, so a deposit stays
        pre-invoice in its collection week even after the invoice posts and
        reconciles later.
        """
        payments = self.env["account.payment"].search(
            self._collected_payment_domain(
                windows[0]["start"], windows[-1]["end"], company)
        )
        pre_invoice_ids = []
        for payment in payments:
            row = self._row_for_date(weekly, windows, payment.date)
            if not row:
                continue
            amount = abs(self._to_company_currency(
                payment.amount, payment.currency_id, payment.date, company))
            row["cash_collected"] += amount
            if self._payment_settles_posted_invoice(payment):
                row["collected_invoiced"] += amount
            else:
                row["collected_pre_invoice"] += amount
                pre_invoice_ids.append(payment.id)
        return {"pre_invoice_payment_ids": pre_invoice_ids}

    @api.model
    def _payment_settles_posted_invoice(self, payment):
        invoices = payment.reconciled_invoice_ids
        orders = payment.sale_order_id
        if "payment_transaction_id" in payment._fields and payment.payment_transaction_id:
            orders |= payment.payment_transaction_id.sale_order_ids
        invoices |= orders.invoice_ids
        return any(
            move.move_type == "out_invoice"
            and move.state == "posted"
            and move.invoice_date
            and move.invoice_date <= payment.date
            for move in invoices
        )

    @api.model
    def _cogs_line_domain(self, start, end, company):
        return [
            ("company_id", "=", company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "expense_direct_cost"),
            ("date", ">=", fields.Date.to_string(start)),
            ("date", "<", fields.Date.to_string(end)),
        ]

    @api.model
    def _top_level_category(self, category):
        """Return the ancestor one level below the root category. A category
        sitting directly on the root (or the root itself) is returned as-is.
        """
        if not category:
            return category
        while category.parent_id and category.parent_id.parent_id:
            category = category.parent_id
        return category

    @api.model
    def _fill_cogs(self, weekly, windows, company):
        """Weekly COGS plus range-level breakdown accumulators from posted
        journal items on Cost of Revenue accounts. Amounts use the signed line
        balance (debit - credit, company currency), so totals equal the P&L
        cost of revenue for the same dates and reversals reduce COGS.
        """
        lines = self.env["account.move.line"].search(
            self._cogs_line_domain(
                windows[0]["start"], windows[-1]["end"], company)
        )
        categories = {}
        accounts = {}
        for line in lines:
            row = self._row_for_date(weekly, windows, line.date)
            if not row:
                continue
            amount = line.balance
            row["cogs"] += amount
            category = self._top_level_category(line.product_id.categ_id)
            cat_key = category.id if category else 0
            if cat_key not in categories:
                categories[cat_key] = {
                    "key": cat_key,
                    "label": category.name if category
                             else _("Other / manual entries"),
                    "amount": 0.0,
                }
            categories[cat_key]["amount"] += amount
            account = line.account_id
            if account.id not in accounts:
                accounts[account.id] = {
                    "id": account.id,
                    "code": account.code or "",
                    "label": account.name,
                    "amount": 0.0,
                }
            accounts[account.id]["amount"] += amount
        return {"categories": categories, "accounts": accounts}

    @api.model
    def _cogs_breakdown_payload(self, cogs_meta, start, end, company):
        """Turn the fill accumulators into sorted payload rows with drilldown
        domains. Category rows carry the share of range COGS; both lists come
        from the same line set as the weekly totals, so they reconcile by
        construction.
        """
        base_domain = self._cogs_line_domain(start, end, company)
        total = sum(
            row["amount"] for row in cogs_meta["categories"].values())
        by_category = []
        for row in sorted(cogs_meta["categories"].values(),
                          key=lambda item: item["amount"], reverse=True):
            if row["key"]:
                domain = base_domain + [
                    ("product_id.categ_id", "child_of", row["key"])]
            else:
                domain = base_domain + [("product_id", "=", False)]
            by_category.append({
                "key": row["key"],
                "label": row["label"],
                "amount": self._round(row["amount"]),
                "percent": self._ratio_percent(row["amount"], total),
                "drilldown": {"model": "account.move.line", "domain": domain},
            })
        by_account = [
            {
                "id": row["id"],
                "code": row["code"],
                "label": row["label"],
                "amount": self._round(row["amount"]),
                "drilldown": {
                    "model": "account.move.line",
                    "domain": base_domain + [("account_id", "=", row["id"])],
                },
            }
            for row in sorted(cogs_meta["accounts"].values(),
                              key=lambda item: item["amount"], reverse=True)
        ]
        return by_category, by_account

    @api.model
    def _inventory_value_payload(self, company):
        """Current on-hand inventory value grouped by stock valuation account.

        Uses Odoo's stock_account valuation helper so the dashboard follows
        product/category/company valuation settings and returns only aggregate
        account rows, not product or quant line listings.
        """
        company = company.with_company(company)
        value_by_account = company.stock_value()
        total = sum(value_by_account.values())
        rows = []
        for account, amount in sorted(
                value_by_account.items(), key=lambda item: item[1],
                reverse=True):
            if company.currency_id.is_zero(amount):
                continue
            account_id = account.id if account else 0
            rows.append({
                "id": account_id,
                "code": account.code if account else "",
                "label": account.name if account
                         else _("Unconfigured inventory account"),
                "display_name": account.display_name if account
                                else _("Unconfigured inventory account"),
                "amount": self._round(amount),
                "percent": self._ratio_percent(amount, total),
            })
        return {
            "total": self._round(total),
            "rows": rows,
        }

    @api.model
    def _inventory_history_windows(self, windows):
        """Use weekly points for short ranges and month-end points for long ones.

        Odoo's historical stock valuation is intentionally authoritative but
        expensive. Long dashboard ranges only need the carrying-value trend, so
        keeping the last reporting bucket in each month preserves the shape
        without running a valuation for every week.
        """
        if len(windows) <= 13:
            return windows, "weekly"
        selected = []
        selected_months = []
        for window in windows:
            at_date = window["end"] - timedelta(days=1)
            month_key = (at_date.year, at_date.month)
            if selected_months and selected_months[-1] == month_key:
                selected[-1] = window
            else:
                selected.append(window)
                selected_months.append(month_key)
        return selected, "monthly"

    @api.model
    def _inventory_history_payload(self, windows, company):
        """Period-end inventory value trend by valuation account.

        This deliberately reuses Odoo's stock valuation helper with at_date so
        the historical trend follows the same product/category valuation rules
        as the current inventory snapshot.
        """
        company = company.with_company(company)
        history_windows, granularity = self._inventory_history_windows(windows)
        accounts_by_product = company._get_accounts_by_product()
        points = []
        account_values = {}
        total_values = []
        for window in history_windows:
            at_date = window["end"] - timedelta(days=1)
            value_by_account = company.stock_value(
                accounts_by_product=accounts_by_product,
                at_date=at_date,
            )
            point_total = 0.0
            for account, amount in value_by_account.items():
                account_id = account.id if account else 0
                if account_id not in account_values:
                    account_values[account_id] = {
                        "account": account,
                        "values": [0.0] * len(points),
                    }
                account_values[account_id]["values"].append(amount)
                point_total += amount
            for data in account_values.values():
                if len(data["values"]) < len(points) + 1:
                    data["values"].append(0.0)
            total_values.append(point_total)
            points.append({
                "key": window["key"],
                "label": window["label"],
                "range_label": window["range_label"],
                "date": fields.Date.to_string(at_date),
                "partial": window.get("partial", False),
            })

        series = [{
            "key": "total",
            "label": _("Total Inventory"),
            "account_code": "",
            "is_total": True,
            "values": [self._round(value) for value in total_values],
        }]
        account_series = []
        for account_id, data in account_values.items():
            account = data["account"]
            values = [self._round(value) for value in data["values"]]
            if not any(not company.currency_id.is_zero(value) for value in values):
                continue
            account_series.append({
                "key": str(account_id),
                "label": account.name if account
                         else _("Unconfigured inventory account"),
                "account_code": account.code if account else "",
                "is_total": False,
                "values": values,
            })
        account_series.sort(
            key=lambda item: item["values"][-1] if item["values"] else 0.0,
            reverse=True,
        )
        series.extend(account_series)
        return {
            "points": points,
            "series": series,
            "granularity": granularity,
        }

    @api.model
    def _fill_operational_finance(self, weekly, windows, company):
        unpaid_orders = self.env["sale.order"].search(
            self._current_open_domains(company)["unpaid_delivery_orders"]["domain"]
        )
        for order in unpaid_orders:
            row = self._row_for_date(
                weekly, windows, fields.Date.to_date(order.date_order)
            )
            if row:
                row["unpaid_delivery_orders"] += self._to_company_currency(
                    order.amount_total,
                    order.currency_id,
                    fields.Date.to_date(order.date_order),
                    company,
                )
                row["unpaid_delivery_count"] += 1

        stripe_domain = [
            ("company_id", "=", company.id),
            ("create_date", ">=", fields.Datetime.to_string(datetime.combine(windows[0]["start"], time.min))),
            ("create_date", "<", fields.Datetime.to_string(datetime.combine(windows[-1]["end"], time.min))),
        ]
        for payment in self.env["stripe.terminal.payment"].search(stripe_domain):
            row = self._row_for_date(weekly, windows, fields.Date.to_date(payment.create_date))
            if not row:
                continue
            if payment.status == "succeeded":
                row["stripe_succeeded"] += 1
            if payment.status in ("failed", "requires_action", "processing"):
                row["stripe_exceptions"] += 1
                row["payment_exceptions"] += 1

    @api.model
    def _sale_orders_for_range(self, start, end, company):
        return self.env["sale.order"].search([
            ("company_id", "=", company.id),
            ("state", "in", ("sale", "done")),
            ("date_order", ">=", fields.Datetime.to_string(datetime.combine(start, time.min))),
            ("date_order", "<", fields.Datetime.to_string(datetime.combine(end, time.min))),
        ])

    @api.model
    def _salesperson_rows(self, start, end, company):
        rows = {}
        for order in self._sale_orders_for_range(start, end, company):
            user = order.user_id
            key = user.id or 0
            if key not in rows:
                rows[key] = {
                    "id": key,
                    "name": user.display_name if user else _("No Salesperson"),
                    "sales": 0.0,
                    "gross_profit": 0.0,
                    "margin_percent": 0.0,
                    "order_count": 0,
                    "low_margin_order_count": 0,
                    "order_ids": [],
                }
            order_date = fields.Date.to_date(order.date_order)
            sales = self._to_company_currency(order.amount_untaxed, order.currency_id, order_date, company)
            margin = self._to_company_currency(order.margin, order.currency_id, order_date, company)
            rows[key]["sales"] += sales
            rows[key]["gross_profit"] += margin
            rows[key]["order_count"] += 1
            rows[key]["order_ids"].append(order.id)
            if order.amount_untaxed and order.margin_percent < self.LOW_MARGIN_THRESHOLD:
                rows[key]["low_margin_order_count"] += 1
        for row in rows.values():
            row["sales"] = self._round(row["sales"])
            row["gross_profit"] = self._round(row["gross_profit"])
            row["margin_percent"] = self._ratio_percent(row["gross_profit"], row["sales"])
            row["drilldown"] = {
                "model": "sale.order",
                "domain": [("id", "in", row.pop("order_ids"))],
            }
        return sorted(rows.values(), key=lambda item: item["sales"], reverse=True)[:12]

    @api.model
    def _fulfillment_conversion_rows(self, start, end, company,
                                     comparison_start=None,
                                     comparison_end=None):
        """Compare quote-to-sale and sale-to-paid cohorts by fulfillment.

        ``sale.order.date_order`` changes from the quotation date to the
        confirmation date when Odoo confirms an order.  ``create_date`` is
        therefore the stable cohort anchor for this analysis.  Conversion and
        payment outcomes are current-state snapshots as of dashboard refresh.
        Canceled quotations remain in the creation cohort so its denominator
        does not shrink as older quotations are closed without a sale.
        """
        comparison_start = comparison_start or start
        comparison_end = comparison_end or end
        cohort_states = ("draft", "sent", "sale", "cancel")
        confirmed_states = ("sale",)
        fulfillment_rows = (
            ("will_call", _("Will Call/Customer Pickup")),
            ("delivery", _("Delivery")),
        )

        def aggregate(period_start, period_end):
            domain = [
                ("company_id", "=", company.id),
                ("state", "in", cohort_states),
                (
                    "create_date",
                    ">=",
                    fields.Datetime.to_string(
                        datetime.combine(period_start, time.min)),
                ),
                (
                    "create_date",
                    "<",
                    fields.Datetime.to_string(
                        datetime.combine(period_end, time.min)),
                ),
            ]
            buckets = {
                key: {
                    "cohort_ids": [],
                    "confirmed_ids": [],
                    "paid_ids": [],
                    "cohort_sales": 0.0,
                    "confirmed_sales": 0.0,
                    "paid_sales": 0.0,
                }
                for key, _label in fulfillment_rows
            }
            buckets[False] = {
                "cohort_ids": [],
                "confirmed_ids": [],
                "paid_ids": [],
                "cohort_sales": 0.0,
                "confirmed_sales": 0.0,
                "paid_sales": 0.0,
            }
            total = {
                "cohort_ids": [],
                "confirmed_ids": [],
                "paid_ids": [],
                "cohort_sales": 0.0,
                "confirmed_sales": 0.0,
                "paid_sales": 0.0,
            }

            for order in self.env["sale.order"].search(domain):
                key = (
                    order.fulfillment_type
                    if order.fulfillment_type in buckets
                    else False
                )
                bucket = buckets[key]
                order_date = fields.Date.to_date(order.date_order)
                order_sales = self._to_company_currency(
                    order.amount_untaxed,
                    order.currency_id,
                    order_date,
                    company,
                )
                bucket["cohort_ids"].append(order.id)
                bucket["cohort_sales"] += order_sales
                total["cohort_ids"].append(order.id)
                total["cohort_sales"] += order_sales
                if order.state not in confirmed_states:
                    continue
                bucket["confirmed_ids"].append(order.id)
                bucket["confirmed_sales"] += order_sales
                total["confirmed_ids"].append(order.id)
                total["confirmed_sales"] += order_sales
                if order.unico_payment_status != "paid":
                    continue
                bucket["paid_ids"].append(order.id)
                bucket["paid_sales"] += order_sales
                total["paid_ids"].append(order.id)
                total["paid_sales"] += order_sales
            return buckets, total

        current, current_total = aggregate(start, end)
        comparison, comparison_total = aggregate(
            comparison_start, comparison_end)

        def drilldown(record_ids):
            if not record_ids:
                return {}
            return {
                "model": "sale.order",
                "domain": [("id", "in", record_ids)],
            }

        def metrics(current_bucket, comparison_bucket):
            current_cohort_count = len(current_bucket["cohort_ids"])
            current_confirmed_count = len(current_bucket["confirmed_ids"])
            current_paid_count = len(current_bucket["paid_ids"])
            comparison_cohort_count = len(comparison_bucket["cohort_ids"])
            comparison_confirmed_count = len(
                comparison_bucket["confirmed_ids"])
            comparison_paid_count = len(comparison_bucket["paid_ids"])
            current_quote_to_sale_rate = self._ratio_percent(
                current_confirmed_count, current_cohort_count)
            comparison_quote_to_sale_rate = self._ratio_percent(
                comparison_confirmed_count, comparison_cohort_count)
            current_sale_to_paid_rate = self._ratio_percent(
                current_paid_count, current_confirmed_count)
            comparison_sale_to_paid_rate = self._ratio_percent(
                comparison_paid_count, comparison_confirmed_count)
            return {
                "current_cohort_count": current_cohort_count,
                "current_confirmed_count": current_confirmed_count,
                "current_paid_count": current_paid_count,
                "current_quote_sales": self._round(
                    current_bucket["cohort_sales"]),
                "current_confirmed_sales": self._round(
                    current_bucket["confirmed_sales"]),
                "current_paid_sales": self._round(
                    current_bucket["paid_sales"]),
                "comparison_cohort_count": comparison_cohort_count,
                "comparison_confirmed_count": comparison_confirmed_count,
                "comparison_paid_count": comparison_paid_count,
                "comparison_quote_sales": self._round(
                    comparison_bucket["cohort_sales"]),
                "comparison_confirmed_sales": self._round(
                    comparison_bucket["confirmed_sales"]),
                "comparison_paid_sales": self._round(
                    comparison_bucket["paid_sales"]),
                "current_quote_to_sale_rate": current_quote_to_sale_rate,
                "comparison_quote_to_sale_rate": comparison_quote_to_sale_rate,
                "quote_to_sale_delta_points": self._round(
                    current_quote_to_sale_rate
                    - comparison_quote_to_sale_rate,
                    2,
                ),
                "current_sale_to_paid_rate": current_sale_to_paid_rate,
                "comparison_sale_to_paid_rate": comparison_sale_to_paid_rate,
                "sale_to_paid_delta_points": self._round(
                    current_sale_to_paid_rate
                    - comparison_sale_to_paid_rate,
                    2,
                ),
                "current_cohort_drilldown": drilldown(
                    current_bucket["cohort_ids"]),
                "current_confirmed_drilldown": drilldown(
                    current_bucket["confirmed_ids"]),
                "current_paid_drilldown": drilldown(
                    current_bucket["paid_ids"]),
                "comparison_cohort_drilldown": drilldown(
                    comparison_bucket["cohort_ids"]),
                "comparison_confirmed_drilldown": drilldown(
                    comparison_bucket["confirmed_ids"]),
                "comparison_paid_drilldown": drilldown(
                    comparison_bucket["paid_ids"]),
            }

        rows = []
        for key, label in fulfillment_rows:
            rows.append({
                "id": key,
                "label": label,
                **metrics(current[key], comparison[key]),
            })
        if current[False]["cohort_ids"] or comparison[False]["cohort_ids"]:
            rows.append({
                "id": "not_set",
                "label": _("Not Set"),
                **metrics(current[False], comparison[False]),
            })

        return {
            "date_start": fields.Date.to_string(start),
            "date_end": fields.Date.to_string(end - timedelta(days=1)),
            "range_label": self._date_range_label(
                start, end - timedelta(days=1)),
            "comparison_date_start": fields.Date.to_string(comparison_start),
            "comparison_date_end": fields.Date.to_string(
                comparison_end - timedelta(days=1)),
            "comparison_label": self._date_range_label(
                comparison_start, comparison_end - timedelta(days=1)),
            "rows": rows,
            "current_total": metrics(current_total, comparison_total),
        }

    @api.model
    def _payment_term_rows(self, start, end, company,
                           comparison_start=None, comparison_end=None):
        """Compare confirmed-sales mix by payment term across two periods.

        This deliberately lives outside ``_fill_sales_quality``: the weekly
        filler runs once for the selected range and again for its comparison
        range. Keeping both queries explicit here prevents the discarded prior
        pass from leaking into the selected-period table.
        """
        comparison_start = comparison_start or start
        comparison_end = comparison_end or end

        def aggregate(period_start, period_end):
            buckets = {}
            total_count = 0
            total_sales = 0.0
            for order in self._sale_orders_for_range(
                    period_start, period_end, company):
                payment_term = order.payment_term_id
                key = payment_term.id or 0
                bucket = buckets.setdefault(key, {
                    "id": key,
                    "label": (
                        payment_term.display_name
                        if payment_term
                        else _("No Payment Terms")
                    ),
                    "order_count": 0,
                    "confirmed_sales": 0.0,
                    "order_ids": [],
                })
                order_date = fields.Date.to_date(order.date_order)
                confirmed_sales = self._to_company_currency(
                    order.amount_untaxed,
                    order.currency_id,
                    order_date,
                    company,
                )
                bucket["order_count"] += 1
                bucket["confirmed_sales"] += confirmed_sales
                bucket["order_ids"].append(order.id)
                total_count += 1
                total_sales += confirmed_sales
            return buckets, {
                "order_count": total_count,
                "confirmed_sales": total_sales,
            }

        current, current_total = aggregate(start, end)
        comparison, comparison_total = aggregate(
            comparison_start, comparison_end)

        def share(bucket, total):
            if not bucket or not total["confirmed_sales"]:
                return 0.0
            return (
                bucket["confirmed_sales"] / total["confirmed_sales"]
            ) * 100.0

        keys = set(current) | set(comparison)
        ordered_keys = sorted(
            keys,
            key=lambda key: (
                -current.get(key, {}).get("confirmed_sales", 0.0),
                -share(comparison.get(key), comparison_total),
                (
                    current.get(key, {}).get("label")
                    or comparison.get(key, {}).get("label")
                    or ""
                ).lower(),
            ),
        )
        rows = []
        for key in ordered_keys:
            current_bucket = current.get(key)
            comparison_bucket = comparison.get(key)
            current_share = share(current_bucket, current_total)
            comparison_share = share(comparison_bucket, comparison_total)
            current_ids = (
                current_bucket["order_ids"] if current_bucket else [])
            comparison_ids = (
                comparison_bucket["order_ids"] if comparison_bucket else [])
            rows.append({
                "id": key,
                "label": (
                    current_bucket["label"]
                    if current_bucket
                    else comparison_bucket["label"]
                ),
                "current_order_count": (
                    current_bucket["order_count"] if current_bucket else 0),
                "current_confirmed_sales": self._round(
                    current_bucket["confirmed_sales"]
                    if current_bucket else 0.0
                ),
                "current_share_percent": self._round(current_share, 2),
                "comparison_share_percent": self._round(
                    comparison_share, 2),
                "share_delta_points": self._round(
                    current_share - comparison_share, 2),
                "current_drilldown": (
                    {
                        "model": "sale.order",
                        "domain": [("id", "in", current_ids)],
                    }
                    if current_ids else {}
                ),
                "comparison_drilldown": (
                    {
                        "model": "sale.order",
                        "domain": [("id", "in", comparison_ids)],
                    }
                    if comparison_ids else {}
                ),
            })

        return {
            "date_start": fields.Date.to_string(start),
            "date_end": fields.Date.to_string(end - timedelta(days=1)),
            "range_label": self._date_range_label(
                start, end - timedelta(days=1)),
            "comparison_date_start": fields.Date.to_string(comparison_start),
            "comparison_date_end": fields.Date.to_string(
                comparison_end - timedelta(days=1)),
            "comparison_label": self._date_range_label(
                comparison_start, comparison_end - timedelta(days=1)),
            "current_total": {
                "order_count": current_total["order_count"],
                "confirmed_sales": self._round(
                    current_total["confirmed_sales"]),
            },
            "comparison_total": {
                "order_count": comparison_total["order_count"],
                "confirmed_sales": self._round(
                    comparison_total["confirmed_sales"]),
            },
            "rows": rows,
        }

    @api.model
    def _low_margin_orders(self, start, end, company):
        orders = self.env["sale.order"].search([
            ("company_id", "=", company.id),
            ("state", "in", ("sale", "done")),
            ("date_order", ">=", fields.Datetime.to_string(datetime.combine(start, time.min))),
            ("date_order", "<", fields.Datetime.to_string(datetime.combine(end, time.min))),
            ("amount_untaxed", ">", 0),
            ("margin_percent", "<", self.LOW_MARGIN_THRESHOLD),
        ], order="margin_percent asc, amount_untaxed desc", limit=12)
        return [
            {
                "id": order.id,
                "name": order.name,
                "customer": order.partner_id.display_name,
                "salesperson": order.user_id.display_name or _("No Salesperson"),
                "sales": self._round(self._to_company_currency(
                    order.amount_untaxed,
                    order.currency_id,
                    fields.Date.to_date(order.date_order),
                    company,
                )),
                "gross_profit": self._round(self._to_company_currency(
                    order.margin,
                    order.currency_id,
                    fields.Date.to_date(order.date_order),
                    company,
                )),
                "margin_percent": self._round(order.margin_percent * 100, 2),
            }
            for order in orders
        ]

    @api.model
    def _rma_exposure_rows(self, company):
        rmas = self.env["rma.main"].search([
            ("company_id", "=", company.id),
            ("state", "in", ("draft", "submit", "approved", "processing")),
        ], order="date desc", limit=12)
        return [
            {
                "id": rma.id,
                "name": rma.name,
                "customer": rma.partner_id.display_name,
                "state": dict(rma._fields["state"].selection).get(rma.state, rma.state),
                "return_value": self._round(rma.total_return),
                "replacement_value": self._round(rma.total_replace),
            }
            for rma in rmas
        ]

    @api.model
    def _stripe_status_rows(self, start, end, company):
        statuses = {}
        for payment in self.env["stripe.terminal.payment"].search([
            ("company_id", "=", company.id),
            ("create_date", ">=", fields.Datetime.to_string(datetime.combine(start, time.min))),
            ("create_date", "<", fields.Datetime.to_string(datetime.combine(end, time.min))),
        ]):
            status = payment.status or "unknown"
            if status not in statuses:
                statuses[status] = {
                    "status": status,
                    "count": 0,
                    "amount": 0.0,
                    "payment_ids": [],
                }
            statuses[status]["count"] += 1
            statuses[status]["payment_ids"].append(payment.id)
            statuses[status]["amount"] += self._to_company_currency(
                payment.amount,
                payment.currency_id,
                fields.Date.to_date(payment.create_date),
                company,
            )
        rows = list(statuses.values())
        for row in rows:
            row["amount"] = self._round(row["amount"])
            row["drilldown"] = {
                "model": "stripe.terminal.payment",
                "domain": [("id", "in", row.pop("payment_ids"))],
            }
        return sorted(rows, key=lambda item: item["count"], reverse=True)

    @api.model
    def _payment_exception_rows(self, company):
        rows = []
        for move in self._payment_exception_moves(company)[:12]:
            rows.append({
                "id": move.id,
                "model": "account.move",
                "name": move.name or move.ref,
                "customer": move.partner_id.display_name,
                "due_date": fields.Date.to_string(move.invoice_date_due) if move.invoice_date_due else "",
                "residual": self._round(abs(move.amount_residual_signed)),
                "state": move.payment_state,
                "status_label": self._payment_exception_status_label(move.payment_state),
            })
        for payment in self.env["stripe.terminal.payment"].search(
            self._current_open_domains(company)["stripe_exceptions"]["domain"],
            order="create_date desc",
            limit=max(0, 12 - len(rows)),
        ):
            rows.append({
                "id": payment.id,
                "model": "stripe.terminal.payment",
                "name": payment.name,
                "customer": payment.partner_id.display_name,
                "due_date": fields.Date.to_string(fields.Date.to_date(payment.create_date)),
                "residual": self._round(self._to_company_currency(
                    payment.amount,
                    payment.currency_id,
                    fields.Date.to_date(payment.create_date),
                    company,
                )),
                "state": "Stripe %s" % (payment.status or "unknown"),
                "status_label": self._stripe_exception_status_label(payment.status),
            })
        return rows

    @api.model
    def _payment_exception_status_label(self, payment_state):
        labels = {
            "not_paid": _("Invoice Not Paid"),
            "partial": _("Invoice Partially Paid"),
        }
        if payment_state in labels:
            return labels[payment_state]
        return (payment_state or _("Unknown")).replace("_", " ").title()

    @api.model
    def _stripe_exception_status_label(self, status):
        labels = {
            "failed": _("Stripe Payment Failed"),
            "processing": _("Stripe Payment Processing"),
        }
        if status in labels:
            return labels[status]
        return _("Stripe Payment %s") % (
            (status or _("Unknown")).replace("_", " ").title()
        )

    @api.model
    def _unpaid_delivery_order_rows(self, company):
        orders = self.env["sale.order"].search(
            self._current_open_domains(company)["unpaid_delivery_orders"]["domain"],
            order="date_order desc",
            limit=12,
        )
        return [
            {
                "id": order.id,
                "name": order.name,
                "customer": order.partner_id.display_name,
                "amount": self._round(self._to_company_currency(
                    order.amount_total,
                    order.currency_id,
                    fields.Date.to_date(order.date_order),
                    company,
                )),
                "date": fields.Date.to_string(fields.Date.to_date(order.date_order)),
            }
            for order in orders
        ]

    @api.model
    def _aging_buckets(self, company):
        today = fields.Date.context_today(self)
        buckets = [
            ("Current", None, 0),
            ("1-30", 1, 30),
            ("31-60", 31, 60),
            ("61-90", 61, 90),
            ("90+", 91, None),
        ]
        rows = {
            label: {"label": label, "amount": 0.0, "count": 0, "move_ids": []}
            for label, _a, _b in buckets
        }
        moves = self.env["account.move"].search(
            self._current_open_domains(company)["unpaid_invoices"]["domain"]
        )
        for move in moves:
            due_date = move.invoice_date_due or move.invoice_date or today
            days = (today - due_date).days
            if days <= 0:
                label = "Current"
            elif days <= 30:
                label = "1-30"
            elif days <= 60:
                label = "31-60"
            elif days <= 90:
                label = "61-90"
            else:
                label = "90+"
            rows[label]["amount"] += abs(move.amount_residual_signed)
            rows[label]["count"] += 1
            rows[label]["move_ids"].append(move.id)
        for row in rows.values():
            row["amount"] = self._round(row["amount"])
        total = sum(row["amount"] for row in rows.values())
        for row in rows.values():
            row["percent"] = self._round(
                (row["amount"] / total * 100.0) if total else 0.0, 1
            )
            move_ids = row.pop("move_ids")
            row["drilldown"] = (
                {"model": "account.move", "domain": [("id", "in", move_ids)]}
                if move_ids else {}
            )
        return [rows[label] for label, _a, _b in buckets]

    @api.model
    def _receivable_line_domain(self, company, sign=None):
        domain = [
            ("company_id", "=", company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0.0),
        ]
        if sign == "positive":
            domain.append(("amount_residual", ">", 0.0))
        elif sign == "negative":
            domain.append(("amount_residual", "<", 0.0))
        return domain

    @api.model
    def _payable_line_domain(self, company, sign=None):
        domain = [
            ("company_id", "=", company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "liability_payable"),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0.0),
        ]
        if sign == "open":
            domain.append(("amount_residual", "<", 0.0))
        elif sign == "credit":
            domain.append(("amount_residual", ">", 0.0))
        return domain

    @api.model
    def _line_due_date(self, line):
        return line.date_maturity or line.date or fields.Date.context_today(self)

    @api.model
    def _line_reference(self, line):
        return line.move_id.name or line.move_id.ref or line.name or _("No Reference")

    @api.model
    def _aging_detail_bucket_defs(self):
        return [
            {"key": "current", "label": _("Current")},
            {"key": "days_1_30", "label": _("1-30")},
            {"key": "days_31_60", "label": _("31-60")},
            {"key": "days_61_90", "label": _("61-90")},
            {"key": "days_91_120", "label": _("91-120")},
            {"key": "older", "label": _("Older")},
        ]

    @api.model
    def _aging_detail_bucket_for_date(self, due_date, today):
        days = (today - due_date).days
        if days <= 0:
            key = "current"
        elif days <= 30:
            key = "days_1_30"
        elif days <= 60:
            key = "days_31_60"
        elif days <= 90:
            key = "days_61_90"
        elif days <= 120:
            key = "days_91_120"
        else:
            key = "older"
        return next(
            bucket for bucket in self._aging_detail_bucket_defs()
            if bucket["key"] == key
        )

    @api.model
    def _customer_tag_payload(self, partner):
        if not partner:
            return []
        categories = partner.category_id.sorted(
            key=lambda category: ((category.display_name or "").casefold(), category.id)
        )
        return [
            {
                "id": category.id,
                "name": category.display_name,
                "color": category.color or 0,
            }
            for category in categories
        ]

    @api.model
    def _ar_offset_reason(self, line):
        values = [
            line.name,
            line.ref,
            line.move_id.name,
            line.move_id.ref,
            line.journal_id.name,
            line.payment_id.name if line.payment_id else "",
        ]
        haystack = " ".join(value for value in values if value).lower()
        if "stripe" in haystack:
            return _("Stripe settlement offset")
        if line.move_id.move_type == "out_refund":
            return _("Customer credit memo")
        if line.payment_id and line.payment_id.partner_type == "customer":
            return _("Unapplied customer payment or overpayment")
        if line.move_id.is_entry():
            return _("Manual receivable adjustment")
        return _("Customer credit or AR offset")

    @api.model
    def _ar_customer_balances(self, company, limit=12):
        balances = {}
        lines = self.env["account.move.line"].search(
            self._receivable_line_domain(company)
        )
        for line in lines:
            partner = line.partner_id.commercial_partner_id or line.partner_id
            key = partner.id or 0
            if key not in balances:
                balances[key] = {
                    "id": key,
                    "customer": partner.display_name if partner else _("No Customer"),
                    "open_amount": 0.0,
                    "offset_amount": 0.0,
                    "net_amount": 0.0,
                    "line_count": 0,
                    "line_ids": [],
                    "status": "",
                }
            amount = line.amount_residual
            if amount > 0:
                balances[key]["open_amount"] += amount
            else:
                balances[key]["offset_amount"] += abs(amount)
            balances[key]["net_amount"] += amount
            balances[key]["line_count"] += 1
            balances[key]["line_ids"].append(line.id)

        rows = []
        for row in balances.values():
            if (
                company.currency_id.is_zero(row["open_amount"])
                and company.currency_id.is_zero(row["offset_amount"])
            ):
                continue
            if row["offset_amount"] and row["open_amount"]:
                row["status"] = _("Open balance with offsets")
            elif row["offset_amount"]:
                row["status"] = _("Credit / offset only")
            else:
                row["status"] = _("Open balance")
            row["open_amount"] = self._round(row["open_amount"])
            row["offset_amount"] = self._round(row["offset_amount"])
            row["net_amount"] = self._round(row["net_amount"])
            row["drilldown"] = {
                "model": "account.move.line",
                "domain": [("id", "in", row.pop("line_ids"))],
            }
            rows.append(row)
        rows = sorted(
            rows,
            key=lambda item: (item["open_amount"], abs(item["net_amount"])),
            reverse=True,
        )
        if limit is None:
            return rows
        return rows[:limit]

    @api.model
    def _ar_offsets_payload(self, company, limit=12):
        lines = self.env["account.move.line"].search(
            self._receivable_line_domain(company, sign="negative"),
            order="date desc, id desc",
        )
        total = sum(abs(line.amount_residual) for line in lines)
        rows = []
        row_lines = lines if limit is None else lines[:limit]
        for line in row_lines:
            amount = abs(line.amount_residual)
            if company.currency_id.is_zero(amount):
                continue
            rows.append({
                "id": line.id,
                "move_id": line.move_id.id,
                "reference": self._line_reference(line),
                "customer": line.partner_id.display_name or _("No Customer"),
                "date": fields.Date.to_string(line.date) if line.date else "",
                "due_date": fields.Date.to_string(self._line_due_date(line)),
                "reason": self._ar_offset_reason(line),
                "amount": self._round(amount),
                "signed_amount": self._round(line.amount_residual),
            })
        return {
            "total": self._round(total),
            "count": len(lines),
            "rows": rows,
        }

    @api.model
    def _ar_aging_report(self, company):
        today = fields.Date.context_today(self)
        buckets = self._aging_detail_bucket_defs()
        bucket_keys = [bucket["key"] for bucket in buckets]
        rows = {}
        totals = {
            "customer": _("Total"),
            "line_count": 0,
            "total": 0.0,
            **{key: 0.0 for key in bucket_keys},
        }
        lines = self.env["account.move.line"].search(
            self._receivable_line_domain(company, sign="positive")
        )
        for line in lines:
            partner = line.partner_id.commercial_partner_id or line.partner_id
            key = partner.id or 0
            if key not in rows:
                tags = self._customer_tag_payload(partner)
                rows[key] = {
                    "id": key,
                    "customer": partner.display_name if partner else _("No Customer"),
                    "tags": tags,
                    "tags_label": ", ".join(tag["name"] for tag in tags),
                    "line_count": 0,
                    "line_ids": [],
                    "total": 0.0,
                    **{bucket_key: 0.0 for bucket_key in bucket_keys},
                }
            bucket_key = self._aging_detail_bucket_for_date(
                self._line_due_date(line), today)["key"]
            amount = line.amount_residual
            rows[key][bucket_key] += amount
            rows[key]["total"] += amount
            rows[key]["line_count"] += 1
            rows[key]["line_ids"].append(line.id)
            totals[bucket_key] += amount
            totals["total"] += amount
            totals["line_count"] += 1

        report_rows = []
        for row in rows.values():
            for bucket_key in bucket_keys:
                row[bucket_key] = self._round(row[bucket_key])
            row["total"] = self._round(row["total"])
            row["drilldown"] = {
                "model": "account.move.line",
                "domain": [("id", "in", row.pop("line_ids"))],
            }
            report_rows.append(row)
        for bucket_key in bucket_keys:
            totals[bucket_key] = self._round(totals[bucket_key])
        totals["total"] = self._round(totals["total"])
        return {
            "buckets": buckets,
            "rows": sorted(report_rows, key=lambda item: item["total"], reverse=True),
            "totals": totals,
        }

    @api.model
    def _ap_aging_buckets(self, company):
        today = fields.Date.context_today(self)
        buckets = self._ap_aging_bucket_defs()
        rows = {
            bucket["label"]: {
                "label": bucket["label"],
                "amount": 0.0,
                "count": 0,
                "line_ids": [],
            }
            for bucket in buckets
        }
        lines = self.env["account.move.line"].search(
            self._payable_line_domain(company, sign="open")
        )
        for line in lines:
            due_date = self._line_due_date(line)
            label = self._ap_aging_bucket_for_date(due_date, today)["label"]
            rows[label]["amount"] += abs(line.amount_residual)
            rows[label]["count"] += 1
            rows[label]["line_ids"].append(line.id)
        for row in rows.values():
            row["amount"] = self._round(row["amount"])
            line_ids = row.pop("line_ids")
            row["drilldown"] = (
                {"model": "account.move.line", "domain": [("id", "in", line_ids)]}
                if line_ids else {}
            )
        return [rows[bucket["label"]] for bucket in buckets]

    @api.model
    def _ap_aging_report(self, company):
        today = fields.Date.context_today(self)
        buckets = self._ap_aging_bucket_defs()
        bucket_keys = [bucket["key"] for bucket in buckets]
        rows = {}
        totals = {
            "vendor": _("Total"),
            "line_count": 0,
            "total": 0.0,
            **{key: 0.0 for key in bucket_keys},
        }
        lines = self.env["account.move.line"].search(
            self._payable_line_domain(company, sign="open")
        )
        for line in lines:
            partner = line.partner_id.commercial_partner_id or line.partner_id
            key = partner.id or 0
            if key not in rows:
                rows[key] = {
                    "id": key,
                    "vendor": partner.display_name if partner else _("No Vendor"),
                    "line_count": 0,
                    "line_ids": [],
                    "total": 0.0,
                    **{bucket_key: 0.0 for bucket_key in bucket_keys},
                }
            bucket_key = self._ap_aging_bucket_for_date(
                self._line_due_date(line), today)["key"]
            amount = abs(line.amount_residual)
            rows[key][bucket_key] += amount
            rows[key]["total"] += amount
            rows[key]["line_count"] += 1
            rows[key]["line_ids"].append(line.id)
            totals[bucket_key] += amount
            totals["total"] += amount
            totals["line_count"] += 1

        report_rows = []
        for row in rows.values():
            for bucket_key in bucket_keys:
                row[bucket_key] = self._round(row[bucket_key])
            row["total"] = self._round(row["total"])
            row["drilldown"] = {
                "model": "account.move.line",
                "domain": [("id", "in", row.pop("line_ids"))],
            }
            report_rows.append(row)
        for bucket_key in bucket_keys:
            totals[bucket_key] = self._round(totals[bucket_key])
        totals["total"] = self._round(totals["total"])
        return {
            "buckets": buckets,
            "rows": sorted(report_rows, key=lambda item: item["total"], reverse=True),
            "totals": totals,
        }

    @api.model
    def _ap_aging_bucket_defs(self):
        return self._aging_detail_bucket_defs()

    @api.model
    def _ap_aging_bucket_for_date(self, due_date, today):
        return self._aging_detail_bucket_for_date(due_date, today)

    @api.model
    def _ap_payable_lines(self, company, limit=12):
        search_kwargs = {"order": "date_maturity asc, date asc, id asc"}
        if limit is not None:
            search_kwargs["limit"] = limit
        lines = self.env["account.move.line"].search(
            self._payable_line_domain(company, sign="open"), **search_kwargs)
        rows = []
        for line in lines:
            amount = abs(line.amount_residual)
            if company.currency_id.is_zero(amount):
                continue
            rows.append({
                "id": line.id,
                "move_id": line.move_id.id,
                "reference": self._line_reference(line),
                "vendor": line.partner_id.display_name or _("No Vendor"),
                "date": fields.Date.to_string(line.date) if line.date else "",
                "due_date": fields.Date.to_string(self._line_due_date(line)),
                "amount": self._round(amount),
            })
        return rows

    @api.model
    def _open_ar_totals(self, company):
        lines = self.env["account.move.line"].search(
            self._receivable_line_domain(company, sign="positive")
        )
        return {
            "amount": self._round(sum(line.amount_residual for line in lines)),
            "count": len(lines),
        }

    @api.model
    def _open_ap_totals(self, company):
        lines = self.env["account.move.line"].search(
            self._payable_line_domain(company, sign="open")
        )
        return {
            "amount": self._round(sum(abs(line.amount_residual) for line in lines)),
            "count": len(lines),
        }

    @api.model
    def _open_delivery_totals(self, company):
        orders = self.env["sale.order"].search(
            self._current_open_domains(company)["unpaid_delivery_orders"]["domain"]
        )
        return {
            "amount": self._round(sum(
                self._to_company_currency(
                    order.amount_total,
                    order.currency_id,
                    fields.Date.to_date(order.date_order),
                    company,
                )
                for order in orders
            )),
            "count": len(orders),
        }

    @api.model
    def _payment_exception_moves(self, company):
        today = fields.Date.context_today(self)
        return self.env["account.move"].search([
            ("company_id", "=", company.id),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
            ("amount_residual", ">", 0),
            ("invoice_date_due", "<", fields.Date.to_string(today)),
        ], order="invoice_date_due asc")

    @api.model
    def _stripe_exception_count(self, company):
        return self.env["stripe.terminal.payment"].search_count(
            self._current_open_domains(company)["stripe_exceptions"]["domain"]
        )

    @api.model
    def _row_for_date(self, weekly, windows, value):
        if not value:
            return None
        value_date = fields.Date.to_date(value)
        for index, window in enumerate(windows):
            if window["start"] <= value_date < window["end"]:
                return weekly[index]
        return None

    @api.model
    def _to_company_currency(self, amount, currency, value_date, company):
        if not currency or currency == company.currency_id:
            return amount or 0.0
        return currency._convert(amount or 0.0, company.currency_id, company, value_date or fields.Date.context_today(self))

    @api.model
    def _currency_payload(self, currency):
        return {
            "id": currency.id,
            "name": currency.name,
            "symbol": currency.symbol,
            "position": currency.position,
            "decimal_places": currency.decimal_places,
        }

    @api.model
    def _ratio_percent(self, numerator, denominator):
        if not denominator:
            return 0.0
        return self._round((numerator / denominator) * 100.0, 2)

    @api.model
    def _percent_change(self, value, prior):
        if not prior:
            return 0.0
        return self._round(((value - prior) / abs(prior)) * 100.0, 2)

    @api.model
    def _round(self, value, digits=2):
        return round(value or 0.0, digits)
