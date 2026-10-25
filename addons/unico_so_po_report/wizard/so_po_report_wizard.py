# -*- coding: utf-8 -*-
import base64
import io
from datetime import datetime, time

import pytz
import xlsxwriter

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# A cancelled purchase is not committed spend, so it never counts against a sale.
CANCELLED_PURCHASE_STATES = ("cancel",)
# Still with the supplier — no agreed order yet. Same reading as unico_wip_report.
RFQ_PURCHASE_STATES = ("draft", "sent", "to approve")


class UnicoSoPoReportWizard(models.TransientModel):
    _name = "unico.so.po.report.wizard"
    _description = "Sales vs Purchases Report"

    # ------------------------------------------------------------------
    # Criteria
    # ------------------------------------------------------------------
    date_from = fields.Date(
        string="From",
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help="Earliest sales order date to include (the order's own date, not "
             "the purchase's).",
    )
    date_to = fields.Date(
        string="To",
        required=True,
        default=lambda self: fields.Date.context_today(self),
        help="Latest sales order date to include. Inclusive: an order placed "
             "late on this day is still in the report.",
    )
    state_filter = fields.Selection(
        [
            ("open", "Open orders (confirmed, not fully delivered)"),
            ("sale", "All confirmed orders"),
            ("quotation", "Quotations only"),
            ("not_cancelled", "Everything except cancelled"),
            ("all", "Everything, cancelled included"),
        ],
        string="Order Status",
        required=True,
        default="open",
        help="Which sales orders to report on. 'Open' is the operations "
             "reading: confirmed by the customer and not yet fully delivered.",
    )
    purchase_scope = fields.Selection(
        [
            ("with_po", "Only orders with purchase orders"),
            ("all", "All orders in the period"),
        ],
        string="Coverage",
        required=True,
        default="with_po",
        help="Keep the default to answer 'which sales have purchases against "
             "them'; switch to all orders to see what was sourced from stock.",
    )
    include_rfq = fields.Boolean(
        string="Count RFQs",
        default=True,
        help="Include purchases the supplier has not confirmed yet (RFQ, RFQ "
             "Sent, To Approve). Uncheck to count only agreed purchase orders.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )

    # ------------------------------------------------------------------
    # On-screen preview — the same numbers the workbook will carry, so the
    # figure can be sanity-checked before anything is downloaded.
    # ------------------------------------------------------------------
    currency_id = fields.Many2one(
        "res.currency",
        string="Currency",
        compute="_compute_preview",
        help="Company currency. Every amount in the report is converted into it.",
    )
    order_count = fields.Integer(string="Orders", compute="_compute_preview")
    purchase_count = fields.Integer(string="Purchase Orders", compute="_compute_preview")
    so_amount_total = fields.Monetary(
        string="Sales (Untaxed)", compute="_compute_preview", currency_field="currency_id")
    po_amount_total = fields.Monetary(
        string="Purchases (Untaxed)", compute="_compute_preview", currency_field="currency_id")
    revenue_total = fields.Monetary(
        string="Revenue", compute="_compute_preview", currency_field="currency_id")

    @api.depends("date_from", "date_to", "state_filter", "purchase_scope",
                 "include_rfq", "company_id")
    def _compute_preview(self):
        for wizard in self:
            wizard.currency_id = wizard.company_id.currency_id
            rows = wizard._report_rows() if wizard.date_from and wizard.date_to else []
            wizard.order_count = len(rows)
            wizard.purchase_count = sum(len(row["purchases"]) for row in rows)
            wizard.so_amount_total = sum(row["so_untaxed"] for row in rows)
            wizard.po_amount_total = sum(row["po_untaxed"] for row in rows)
            wizard.revenue_total = sum(row["revenue"] for row in rows)

    @api.constrains("date_from", "date_to")
    def _check_period(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise ValidationError(
                    _("The start of the period must come before its end."))

    # ------------------------------------------------------------------
    # Gathering
    # ------------------------------------------------------------------
    def _period_bounds(self):
        """The period as UTC datetimes, from the *user's* midnight to midnight.

        ``date_order`` is stored in UTC, so comparing it against bare dates
        would drop orders placed late in the day on the last date of a range.
        """
        self.ensure_one()
        tz = pytz.timezone(self.env.user.tz or "UTC")
        start = tz.localize(datetime.combine(self.date_from, time.min))
        end = tz.localize(datetime.combine(self.date_to, time.max))
        return (
            start.astimezone(pytz.UTC).replace(tzinfo=None),
            end.astimezone(pytz.UTC).replace(tzinfo=None),
        )

    def _sale_domain(self):
        self.ensure_one()
        start, end = self._period_bounds()
        domain = [
            ("company_id", "=", self.company_id.id),
            ("date_order", ">=", fields.Datetime.to_string(start)),
            ("date_order", "<=", fields.Datetime.to_string(end)),
        ]
        if self.state_filter == "open":
            domain += [("state", "=", "sale"), ("delivery_status", "!=", "full")]
        elif self.state_filter == "sale":
            domain += [("state", "=", "sale")]
        elif self.state_filter == "quotation":
            domain += [("state", "in", ("draft", "sent"))]
        elif self.state_filter == "not_cancelled":
            domain += [("state", "!=", "cancel")]
        return domain

    def _eligible_purchases(self, purchases):
        """The purchases that count, given the wizard's settings."""
        self.ensure_one()
        if not purchases:
            return self.env["purchase.order"]
        purchases = purchases.filtered(
            lambda p: p.state not in CANCELLED_PURCHASE_STATES)
        if not self.include_rfq:
            purchases = purchases.filtered(
                lambda p: p.state not in RFQ_PURCHASE_STATES)
        return purchases

    def _to_company_currency(self, amount, currency, date):
        """``amount`` in the company currency, converted at ``date``."""
        self.ensure_one()
        company_currency = self.company_id.currency_id
        if not amount or not currency or currency == company_currency:
            return amount
        return currency._convert(
            amount, company_currency, self.company_id,
            date or fields.Date.context_today(self))

    def _report_rows(self):
        """One dict per reported sales order, purchases already resolved.

        Sorted by revenue, worst first: an order whose purchases have eaten the
        sale is what the reader needs to see at the top.
        """
        self.ensure_one()
        orders = self.env["sale.order"].search(self._sale_domain())
        if not orders:
            return []
        # One batched search for the whole set, rather than a query per order.
        purchase_map = orders._unico_wip_purchase_map()
        rows = []
        for order in orders:
            purchases = self._eligible_purchases(purchase_map.get(order.id))
            if self.purchase_scope == "with_po" and not purchases:
                continue
            order_date = order.date_order and order.date_order.date()
            so_untaxed = self._to_company_currency(
                order.amount_untaxed, order.currency_id, order_date)
            so_total = self._to_company_currency(
                order.amount_total, order.currency_id, order_date)
            po_untaxed = po_total = 0.0
            for purchase in purchases:
                po_date = purchase.date_order and purchase.date_order.date()
                po_untaxed += self._to_company_currency(
                    purchase.amount_untaxed, purchase.currency_id, po_date)
                po_total += self._to_company_currency(
                    purchase.amount_total, purchase.currency_id, po_date)
            revenue = so_untaxed - po_untaxed
            rows.append({
                "order": order,
                "purchases": purchases,
                "so_untaxed": so_untaxed,
                "so_total": so_total,
                "po_untaxed": po_untaxed,
                "po_total": po_total,
                "revenue": revenue,
                "revenue_percent": (revenue / so_untaxed) if so_untaxed else 0.0,
                # sale_margin's own cost-based figure, converted the same way,
                # so the two margin readings sit in the same currency.
                "odoo_margin": self._to_company_currency(
                    order.margin or 0.0, order.currency_id, order_date),
                "po_names": purchases.mapped("name"),
                # Deduplicated, order preserved, so the column reads the way the
                # purchases were raised.
                "vendors": list(dict.fromkeys(purchases.mapped("partner_id.name"))),
            })
        return sorted(rows, key=lambda row: row["revenue"])

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_orders(self):
        """The same selection, in Odoo, for anyone who would rather click."""
        self.ensure_one()
        rows = self._report_rows()
        if not rows:
            raise UserError(self._no_match_message())
        return {
            "type": "ir.actions.act_window",
            "name": _("Sales Orders with Purchases"),
            "res_model": "sale.order",
            "view_mode": "list,form",
            "domain": [("id", "in", [row["order"].id for row in rows])],
            "context": {"create": False},
        }

    def action_export_xlsx(self):
        self.ensure_one()
        rows = self._report_rows()
        if not rows:
            raise UserError(self._no_match_message())
        attachment = self.env["ir.attachment"].create({
            "name": "unico_sales_vs_purchases_%s_%s.xlsx" % (
                self.date_from, self.date_to),
            "datas": base64.b64encode(self._build_workbook(rows)),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
        })
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def _no_match_message(self):
        self.ensure_one()
        if self.purchase_scope == "with_po":
            return _(
                "No sales orders between %(start)s and %(end)s have purchase "
                "orders attached. Widen the dates, change the status filter, or "
                "switch Coverage to all orders.",
                start=self.date_from, end=self.date_to)
        return _(
            "No sales orders match between %(start)s and %(end)s.",
            start=self.date_from, end=self.date_to)

    # ------------------------------------------------------------------
    # Workbook
    # ------------------------------------------------------------------
    def _state_label(self, model_name, value):
        selection = dict(
            self.env[model_name]._fields["state"]._description_selection(self.env))
        return selection.get(value, value or "")

    def _build_workbook(self, rows):
        self.ensure_one()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        fmt = self._workbook_formats(workbook)
        self._write_summary_sheet(workbook, fmt, rows)
        self._write_detail_sheet(workbook, fmt, rows)
        self._write_vendor_sheet(workbook, fmt, rows)
        workbook.close()
        return output.getvalue()

    def _workbook_formats(self, workbook):
        currency = self.company_id.currency_id
        money = "#,##0.%s" % ("0" * (currency.decimal_places or 2))
        return {
            "title": workbook.add_format({"bold": True, "font_size": 14}),
            "note": workbook.add_format({"font_size": 9, "italic": True,
                                         "font_color": "#666666"}),
            "header": workbook.add_format({
                "bold": True, "bg_color": "#F2F2F2", "border": 1,
                "text_wrap": True, "valign": "vcenter"}),
            "text": workbook.add_format({"border": 1}),
            "date": workbook.add_format({"border": 1, "num_format": "yyyy-mm-dd"}),
            "money": workbook.add_format({"border": 1, "num_format": money}),
            "percent": workbook.add_format({"border": 1, "num_format": "0.0%"}),
            "int": workbook.add_format({"border": 1, "num_format": "0"}),
            "total_text": workbook.add_format({"bold": True, "border": 1,
                                               "bg_color": "#F2F2F2"}),
            "total_money": workbook.add_format({
                "bold": True, "border": 1, "bg_color": "#F2F2F2",
                "num_format": money}),
            "total_int": workbook.add_format({"bold": True, "border": 1,
                                              "bg_color": "#F2F2F2",
                                              "num_format": "0"}),
            "total_percent": workbook.add_format({"bold": True, "border": 1,
                                                  "bg_color": "#F2F2F2",
                                                  "num_format": "0.0%"}),
        }

    def _sheet_preamble(self, sheet, fmt, title, rows):
        """Title, criteria and the definition of Revenue, above every table."""
        state_label = dict(self._fields["state_filter"]._description_selection(
            self.env)).get(self.state_filter, self.state_filter)
        sheet.write(0, 0, title, fmt["title"])
        sheet.write(1, 0, _(
            "%(start)s to %(end)s · %(status)s · %(rfq)s · %(count)s orders",
            start=self.date_from, end=self.date_to, status=state_label,
            rfq=_("RFQs counted") if self.include_rfq
                else _("confirmed purchases only"),
            count=len(rows)), fmt["note"])
        sheet.write(2, 0, _(
            "Revenue = sales untaxed − linked purchases untaxed, in %s. "
            "Cancelled purchases are excluded.",
            self.company_id.currency_id.name), fmt["note"])

    def _write_table_header(self, sheet, fmt, headers, row_index=4):
        for column, (label, width) in enumerate(headers):
            sheet.write(row_index, column, label, fmt["header"])
            sheet.set_column(column, column, width)
        sheet.freeze_panes(row_index + 1, 0)
        return row_index + 1

    def _local_date(self, value):
        """A naive date in the user's timezone, for xlsxwriter to format."""
        if not value:
            return None
        return fields.Datetime.context_timestamp(self, value).date()

    def _write_summary_sheet(self, workbook, fmt, rows):
        sheet = workbook.add_worksheet(_("Sales vs Purchases"))
        self._sheet_preamble(sheet, fmt, _("Sales Orders with Purchase Orders"), rows)
        headers = [
            (_("Sales Order"), 14), (_("Customer"), 32), (_("Salesperson"), 20),
            (_("Order Date"), 12), (_("Status"), 14), (_("Delivery"), 14),
            (_("Invoicing"), 16), (_("Sale Untaxed"), 14), (_("Sale Total"), 14),
            (_("POs"), 6), (_("Purchase Orders"), 26), (_("Vendors"), 40),
            (_("Purchase Untaxed"), 16), (_("Revenue"), 14), (_("Revenue %"), 11),
            (_("Odoo Margin"), 14),
        ]
        row_index = self._write_table_header(sheet, fmt, headers)
        first_data_row = row_index
        delivery_labels = dict(
            self.env["sale.order"]._fields["delivery_status"]
            ._description_selection(self.env))
        invoice_labels = dict(
            self.env["sale.order"]._fields["invoice_status"]
            ._description_selection(self.env))
        for row in rows:
            order = row["order"]
            sheet.write(row_index, 0, order.name or "", fmt["text"])
            sheet.write(row_index, 1, order.partner_id.display_name or "", fmt["text"])
            sheet.write(row_index, 2, order.user_id.name or "", fmt["text"])
            order_date = self._local_date(order.date_order)
            if order_date:
                sheet.write_datetime(row_index, 3, order_date, fmt["date"])
            else:
                sheet.write(row_index, 3, "", fmt["date"])
            sheet.write(row_index, 4,
                        self._state_label("sale.order", order.state), fmt["text"])
            sheet.write(row_index, 5,
                        delivery_labels.get(order.delivery_status, ""), fmt["text"])
            sheet.write(row_index, 6,
                        invoice_labels.get(order.invoice_status, ""), fmt["text"])
            sheet.write_number(row_index, 7, row["so_untaxed"], fmt["money"])
            sheet.write_number(row_index, 8, row["so_total"], fmt["money"])
            sheet.write_number(row_index, 9, len(row["purchases"]), fmt["int"])
            sheet.write(row_index, 10, ", ".join(row["po_names"]), fmt["text"])
            sheet.write(row_index, 11, ", ".join(row["vendors"]), fmt["text"])
            sheet.write_number(row_index, 12, row["po_untaxed"], fmt["money"])
            sheet.write_number(row_index, 13, row["revenue"], fmt["money"])
            sheet.write_number(row_index, 14, row["revenue_percent"], fmt["percent"])
            sheet.write_number(row_index, 15, row["odoo_margin"], fmt["money"])
            row_index += 1

        sheet.write(row_index, 0, _("TOTAL"), fmt["total_text"])
        for column in (1, 2, 3, 4, 5, 6, 10, 11):
            sheet.write(row_index, column, "", fmt["total_text"])
        sheet.write_number(row_index, 7,
                           sum(r["so_untaxed"] for r in rows), fmt["total_money"])
        sheet.write_number(row_index, 8,
                           sum(r["so_total"] for r in rows), fmt["total_money"])
        sheet.write_number(row_index, 9,
                           sum(len(r["purchases"]) for r in rows), fmt["total_int"])
        sheet.write_number(row_index, 12,
                           sum(r["po_untaxed"] for r in rows), fmt["total_money"])
        sheet.write_number(row_index, 13,
                           sum(r["revenue"] for r in rows), fmt["total_money"])
        sales_total = sum(r["so_untaxed"] for r in rows)
        sheet.write_number(
            row_index, 14,
            (sum(r["revenue"] for r in rows) / sales_total) if sales_total else 0.0,
            fmt["total_percent"])
        sheet.write_number(row_index, 15,
                           sum(r["odoo_margin"] for r in rows), fmt["total_money"])
        if rows:
            sheet.autofilter(first_data_row - 1, 0, row_index - 1, len(headers) - 1)

    def _write_detail_sheet(self, workbook, fmt, rows):
        sheet = workbook.add_worksheet(_("Purchase Detail"))
        self._sheet_preamble(sheet, fmt, _("Purchases behind each Sales Order"), rows)
        headers = [
            (_("Sales Order"), 14), (_("Customer"), 32), (_("Purchase Order"), 16),
            (_("Vendor"), 32), (_("PO Status"), 16), (_("PO Date"), 12),
            (_("Receipt Status"), 16), (_("Source Document"), 26),
            (_("PO Untaxed"), 14), (_("PO Total"), 14),
        ]
        row_index = self._write_table_header(sheet, fmt, headers)
        first_data_row = row_index
        receipt_labels = dict(
            self.env["purchase.order"]._fields["receipt_status"]
            ._description_selection(self.env))
        po_untaxed_total = po_total_total = 0.0
        for row in rows:
            order = row["order"]
            for purchase in row["purchases"]:
                po_date = purchase.date_order and purchase.date_order.date()
                untaxed = self._to_company_currency(
                    purchase.amount_untaxed, purchase.currency_id, po_date)
                total = self._to_company_currency(
                    purchase.amount_total, purchase.currency_id, po_date)
                po_untaxed_total += untaxed
                po_total_total += total
                sheet.write(row_index, 0, order.name or "", fmt["text"])
                sheet.write(row_index, 1, order.partner_id.display_name or "",
                            fmt["text"])
                sheet.write(row_index, 2, purchase.name or "", fmt["text"])
                sheet.write(row_index, 3, purchase.partner_id.display_name or "",
                            fmt["text"])
                sheet.write(row_index, 4,
                            self._state_label("purchase.order", purchase.state),
                            fmt["text"])
                local_date = self._local_date(purchase.date_order)
                if local_date:
                    sheet.write_datetime(row_index, 5, local_date, fmt["date"])
                else:
                    sheet.write(row_index, 5, "", fmt["date"])
                sheet.write(row_index, 6,
                            receipt_labels.get(purchase.receipt_status, ""),
                            fmt["text"])
                sheet.write(row_index, 7, purchase.origin or "", fmt["text"])
                sheet.write_number(row_index, 8, untaxed, fmt["money"])
                sheet.write_number(row_index, 9, total, fmt["money"])
                row_index += 1

        sheet.write(row_index, 0, _("TOTAL"), fmt["total_text"])
        for column in range(1, 8):
            sheet.write(row_index, column, "", fmt["total_text"])
        sheet.write_number(row_index, 8, po_untaxed_total, fmt["total_money"])
        sheet.write_number(row_index, 9, po_total_total, fmt["total_money"])
        if row_index > first_data_row:
            sheet.autofilter(first_data_row - 1, 0, row_index - 1, len(headers) - 1)

    def _write_vendor_sheet(self, workbook, fmt, rows):
        sheet = workbook.add_worksheet(_("Vendor Summary"))
        self._sheet_preamble(sheet, fmt, _("Spend by Vendor"), rows)
        headers = [
            (_("Vendor"), 36), (_("Purchase Orders"), 16),
            (_("Sales Orders Served"), 20), (_("PO Untaxed"), 16),
            (_("Share of Purchases"), 18),
        ]
        row_index = self._write_table_header(sheet, fmt, headers)
        first_data_row = row_index

        by_vendor = {}
        for row in rows:
            for purchase in row["purchases"]:
                vendor = purchase.partner_id
                entry = by_vendor.setdefault(vendor.id, {
                    "name": vendor.display_name or "",
                    "po_count": 0,
                    "orders": set(),
                    "untaxed": 0.0,
                })
                po_date = purchase.date_order and purchase.date_order.date()
                entry["po_count"] += 1
                entry["orders"].add(row["order"].id)
                entry["untaxed"] += self._to_company_currency(
                    purchase.amount_untaxed, purchase.currency_id, po_date)
        grand_total = sum(entry["untaxed"] for entry in by_vendor.values())

        for entry in sorted(by_vendor.values(), key=lambda e: -e["untaxed"]):
            sheet.write(row_index, 0, entry["name"], fmt["text"])
            sheet.write_number(row_index, 1, entry["po_count"], fmt["int"])
            sheet.write_number(row_index, 2, len(entry["orders"]), fmt["int"])
            sheet.write_number(row_index, 3, entry["untaxed"], fmt["money"])
            sheet.write_number(
                row_index, 4,
                (entry["untaxed"] / grand_total) if grand_total else 0.0,
                fmt["percent"])
            row_index += 1

        sheet.write(row_index, 0, _("TOTAL"), fmt["total_text"])
        sheet.write_number(row_index, 1,
                           sum(e["po_count"] for e in by_vendor.values()),
                           fmt["total_int"])
        sheet.write(row_index, 2, "", fmt["total_text"])
        sheet.write_number(row_index, 3, grand_total, fmt["total_money"])
        sheet.write(row_index, 4, "", fmt["total_text"])
        if row_index > first_data_row:
            sheet.autofilter(first_data_row - 1, 0, row_index - 1, len(headers) - 1)
