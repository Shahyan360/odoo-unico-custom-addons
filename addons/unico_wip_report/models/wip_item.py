# -*- coding: utf-8 -*-
from datetime import datetime, time

from dateutil.relativedelta import relativedelta
from pytz import UTC, timezone

from odoo import _, api, fields, models
from odoo.tools.date_utils import end_of, start_of
from odoo.tools.misc import format_date

# Promised within this many days (and not past) counts as "at risk".
WIP_AT_RISK_WINDOW_DAYS = 3

# How many orders the dashboard watchlist shows.
WIP_WATCHLIST_LIMIT = 15

# Which date the dashboard's period filter applies to. Whitelisted rather than
# taken from the client verbatim: the value goes straight into a domain.
WIP_DATE_FIELDS = [
    ("expected_date", "Expected Delivery"),
    ("order_date", "Order Date"),
]

# "All Time" is deliberately the default. A WIP board that hides open work by
# default would bury exactly what it exists to surface — an order that went
# overdue three months ago.
WIP_PERIODS = [
    ("all", "All Time"),
    ("this_week", "This Week"),
    ("last_week", "Last Week"),
    ("this_month", "This Month"),
    ("last_month", "Last Month"),
    ("custom", "Custom Range"),
]


class UnicoWipItem(models.Model):
    """One work-in-progress tracker per open customer order.

    Deliberately a model of its own rather than more fields on ``sale.order``:
    the manual layer here (stage pinning, blocker notes, escalation ownership)
    is operations data, not sales data, and keeping it separate means an ops
    coordinator can run the board without write access to sale orders.

    Everything factual is a ``related`` to the order or recomputed from it, so
    there is no second copy of the truth to drift — only the manual fields and
    the resolved stage actually originate here.
    """

    _name = "unico.wip.item"
    _description = "Work in Progress Item"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "delay_days desc, expected_date asc, id desc"
    _rec_name = "order_id"

    order_id = fields.Many2one(
        "sale.order",
        string="Customer Order",
        required=True,
        ondelete="cascade",
        index=True,
    )
    active = fields.Boolean(
        default=True,
        help="Archived once the order is fully delivered or cancelled — the "
             "history stays, it just leaves the live board.",
    )

    # ------------------------------------------------------------------
    # Stage: detected automatically, pinnable by hand
    # ------------------------------------------------------------------
    stage_id = fields.Many2one(
        "unico.wip.stage",
        string="Stage",
        group_expand="_group_expand_stage_id",
        compute="_compute_stage_id",
        store=True,
        readonly=False,          # kanban drag writes it directly
        tracking=True,
        index=True,
    )
    auto_stage_id = fields.Many2one(
        "unico.wip.stage",
        string="Detected Stage",
        compute="_compute_stage_id",
        store=True,
        help="Where the system reckons this order actually is, from its "
             "purchases and delivery progress. When the stage has been pinned "
             "by hand this is what it would otherwise have been.",
    )
    stage_override = fields.Boolean(
        string="Stage Pinned",
        copy=False,
        tracking=True,
        help="Set automatically when someone moves the card by hand, which "
             "stops the detector from moving it back. Clear it to hand control "
             "of this order's stage back to the system.",
    )
    stage_disagrees = fields.Boolean(
        string="Pinned Away From Detected",
        compute="_compute_stage_disagrees",
        # Stored so it can be filtered on and counted: it backs a search filter
        # and a dashboard tile, and all three fields it derives from are stored.
        store=True,
        index=True,
        help="The stage was pinned by hand and no longer matches what the "
             "system detects — worth a look.",
    )

    # ------------------------------------------------------------------
    # Ownership & escalation — the reason this model exists
    # ------------------------------------------------------------------
    owner_id = fields.Many2one(
        "res.users",
        string="Responsible",
        tracking=True,
        help="Who is chasing this order. Defaults to the order's salesperson "
             "but can be reassigned to whoever actually owns the follow-up.",
    )
    blocked = fields.Boolean(
        string="Blocked",
        tracking=True,
        help="Flag the order as blocked/escalated so it surfaces on the "
             "dashboard watchlist regardless of its stage.",
    )
    blocker_note = fields.Text(
        string="Delay / Blocker Note",
        tracking=True,
        help="What is holding this order up and who is chasing it.",
    )

    # ------------------------------------------------------------------
    # Facts about the order — related, so there is no second copy to drift.
    # Stored only where the board needs to sort, group or aggregate in SQL.
    # ------------------------------------------------------------------
    partner_id = fields.Many2one(
        related="order_id.partner_id", store=True, index=True, string="Customer")
    user_id = fields.Many2one(
        related="order_id.user_id", store=True, string="Salesperson")
    company_id = fields.Many2one(
        related="order_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="order_id.currency_id", store=True)
    amount_untaxed = fields.Monetary(
        related="order_id.amount_untaxed", store=True, string="Sales Value")
    amount_total = fields.Monetary(related="order_id.amount_total", store=True)
    margin = fields.Monetary(
        related="order_id.margin", store=True, string="Expected Margin")
    margin_percent = fields.Float(
        related="order_id.margin_percent", store=True, string="Margin %")
    payment_status = fields.Selection(
        related="order_id.unico_payment_status", store=True,
        string="Payment Status")
    delivery_status = fields.Selection(
        related="order_id.delivery_status", store=True, string="Delivery Status")
    fulfillment_type = fields.Selection(
        related="order_id.fulfillment_type", string="Fulfillment Type")
    commitment_date = fields.Datetime(
        related="order_id.commitment_date", string="Commitment Date")
    order_state = fields.Selection(related="order_id.state", string="Order Status")
    order_date = fields.Datetime(
        related="order_id.date_order", store=True, index=True,
        string="Order Date",
        help="When the customer confirmed. Stored so the dashboard can filter "
             "and group on it without traversing to the order.")

    # ------------------------------------------------------------------
    # Derived from the order (needs work, so computed + stored)
    # ------------------------------------------------------------------
    product_summary = fields.Char(
        string="Products Ordered",
        compute="_compute_commercial",
        compute_sudo=True,
        store=True,
    )
    estimated_cost = fields.Monetary(
        string="Estimated Cost",
        currency_field="currency_id",
        compute="_compute_commercial",
        # sale.order.line.purchase_price is restricted to base.group_user; the
        # roll-up must still compute for a user without that right.
        compute_sudo=True,
        store=True,
        help="Cost of the quantities ORDERED (line cost x ordered qty). Unlike "
             "the delivered-basis margin cost this stays meaningful while the "
             "order is still in progress and nothing has been delivered yet.",
    )
    purchase_ids = fields.Many2many(
        "purchase.order",
        relation="unico_wip_item_purchase_rel",
        column1="item_id",
        column2="purchase_id",
        string="Purchases",
        compute="_compute_procurement",
        compute_sudo=True,
        store=True,
    )
    po_numbers = fields.Char(
        string="PO Number(s)", compute="_compute_procurement",
        compute_sudo=True, store=True)
    supplier_names = fields.Char(
        string="Supplier(s)", compute="_compute_procurement",
        compute_sudo=True, store=True)
    po_count = fields.Integer(
        string="Purchases", compute="_compute_procurement",
        compute_sudo=True, store=True)

    expected_date = fields.Datetime(
        string="Expected Delivery",
        compute="_compute_schedule",
        store=True,
        index=True,
        help="The date the customer is promised: the commitment date when one "
             "is set, otherwise the order's computed expected date.",
    )
    delay_days = fields.Integer(
        string="Days Late",
        compute="_compute_schedule",
        store=True,
        help="Days past the expected delivery date. 0 while still inside the "
             "promise.",
    )
    urgency = fields.Selection(
        selection=[
            ("overdue", "Overdue"),
            ("at_risk", "At Risk"),
            ("on_track", "On Track"),
        ],
        string="Urgency",
        compute="_compute_schedule",
        store=True,
        index=True,
        default="on_track",
    )

    _sql_constraints = [
        ("unico_wip_item_order_uniq", "unique(order_id)",
         "This sales order already has a WIP tracker."),
    ]

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends(
        "order_id.state",
        "order_id.delivery_status",
        # Stored relation, so CPQ special-order purchases keep the stage live.
        "order_id.order_line.cpq_special_purchase_ids",
        "order_id.order_line.cpq_special_purchase_ids.state",
        "order_id.order_line.cpq_special_purchase_ids.receipt_status",
        "stage_override",
    )
    def _compute_stage_id(self):
        """Resolve the detected stage, and adopt it unless the stage is pinned.

        Origin-matched purchases have no dependency path (``origin`` is a Char),
        so the hourly cron re-runs this; the CPQ relation above keeps the common
        special-order case live in between.
        """
        stages = self.env["unico.wip.stage"]._unico_stage_by_token()
        fallback = self.env["unico.wip.stage"]._unico_default_stage()
        purchase_map = self.order_id._unico_wip_purchase_map()
        for item in self:
            token = item.order_id._unico_wip_stage_token(
                purchase_map.get(item.order_id.id))
            auto = stages.get(token) or fallback
            item.auto_stage_id = auto
            # A pinned stage is left exactly where the user put it.
            if item.stage_override:
                item.stage_id = item.stage_id or auto
            else:
                item.stage_id = auto

    @api.depends("stage_id", "auto_stage_id", "stage_override")
    def _compute_stage_disagrees(self):
        for item in self:
            item.stage_disagrees = bool(
                item.stage_override
                and item.auto_stage_id
                and item.stage_id != item.auto_stage_id
            )

    @api.depends(
        "order_id.order_line.display_type",
        "order_id.order_line.product_id",
        "order_id.order_line.product_uom_qty",
        "order_id.order_line.purchase_price",
    )
    def _compute_commercial(self):
        for item in self:
            lines = item.order_id.order_line.filtered(
                lambda line: not line.display_type and line.product_id
            )
            item.product_summary = ", ".join(
                "%s x %s" % (
                    int(line.product_uom_qty)
                    if float(line.product_uom_qty).is_integer()
                    else line.product_uom_qty,
                    line.product_id.display_name,
                )
                for line in lines
            )
            item.estimated_cost = sum(
                line.purchase_price * line.product_uom_qty for line in lines
            )

    @api.depends(
        "order_id.order_line.cpq_special_purchase_ids",
        "order_id.order_line.cpq_special_purchase_ids.state",
    )
    def _compute_procurement(self):
        purchase_map = self.order_id._unico_wip_purchase_map()
        for item in self:
            purchases = item.order_id._unico_wip_open_purchases(
                purchase_map.get(item.order_id.id))
            item.purchase_ids = [fields.Command.set(purchases.ids)]
            item.po_count = len(purchases)
            item.po_numbers = ", ".join(purchases.mapped("name"))
            # De-duplicated but order-preserving: two POs on the same vendor
            # should read as one supplier, not two.
            item.supplier_names = ", ".join(
                dict.fromkeys(purchases.mapped("partner_id.name")))

    @api.depends(
        "order_id.state",
        "order_id.commitment_date",
        "order_id.expected_date",
    )
    def _compute_schedule(self):
        """Promise date, lateness and the urgency bucket.

        ``expected_date`` is not stored upstream (it moves with today's date),
        and lateness is time-relative by definition — the cron re-buckets both.
        """
        today = fields.Date.context_today(self)
        for item in self:
            order = item.order_id
            due = order.commitment_date or order.expected_date
            item.expected_date = due or False
            days = order._unico_wip_days_to_due(due, today) if due else None
            item.delay_days = -days if days is not None and days < 0 else 0
            if days is None:
                item.urgency = "on_track"
            elif days < 0:
                item.urgency = "overdue"
            elif days <= WIP_AT_RISK_WINDOW_DAYS:
                item.urgency = "at_risk"
            else:
                item.urgency = "on_track"

    @api.model
    def _group_expand_stage_id(self, stages, domain):
        """Show every stage as a kanban column, even the empty ones."""
        return self.env["unico.wip.stage"].search([])

    # ------------------------------------------------------------------
    # Overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("owner_id") and vals.get("order_id"):
                order = self.env["sale.order"].browse(vals["order_id"])
                vals["owner_id"] = order.user_id.id or False
        return super().create(vals_list)

    def write(self, vals):
        # Moving a card on the kanban writes stage_id directly. Treat that as
        # pinning the stage, so the detector stops dragging it back on the next
        # recompute. Stored computes assign through the ORM's internal write
        # path, not this one, so they never trip this.
        if "stage_id" in vals and "stage_override" not in vals:
            vals = dict(vals, stage_override=True)
        return super().write(vals)

    def action_clear_stage_override(self):
        """Hand stage control back to the detector."""
        self.write({"stage_override": False})
        self.invalidate_recordset(["stage_id", "auto_stage_id"])
        for fname in ("stage_id", "auto_stage_id"):
            self.env.add_to_compute(self._fields[fname], self)
        self.flush_recordset(["stage_id", "auto_stage_id"])
        return True

    def action_open_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.order_id.display_name,
            "res_model": "sale.order",
            "res_id": self.order_id.id,
            # Pinned explicitly rather than relying on view_mode alone, so the
            # action always lands on the order form and never on a list.
            "views": [(False, "form")],
            "view_mode": "form",
            "target": "current",
        }

    def action_view_purchases(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Purchases for %s", self.order_id.name),
            "res_model": "purchase.order",
            "view_mode": "list,form",
            "domain": [("id", "in", self.purchase_ids.ids)],
            "context": {"create": False},
        }

    # ------------------------------------------------------------------
    # Refresh (cron + manual)
    # ------------------------------------------------------------------
    _REFRESH_FIELDS = (
        "stage_id", "auto_stage_id", "stage_disagrees", "purchase_ids",
        "po_numbers", "supplier_names", "po_count", "expected_date",
        "delay_days", "urgency",
    )

    def _refresh(self):
        """Force a recompute of the fields that cannot stay live on their own
        (origin-matched purchases, and the time-relative urgency)."""
        if not self:
            return
        fields_to_refresh = list(self._REFRESH_FIELDS)
        self.invalidate_recordset(fields_to_refresh)
        for fname in fields_to_refresh:
            self.env.add_to_compute(self._fields[fname], self)
        self.flush_recordset(fields_to_refresh)

    @api.model
    def _cron_refresh_wip(self):
        """Keep the board honest: create trackers for newly confirmed orders,
        archive the finished ones, and re-evaluate the rest."""
        self.env["sale.order"]._unico_sync_wip_items()
        self.search([])._refresh()

    # ------------------------------------------------------------------
    # Period filter
    # ------------------------------------------------------------------
    @api.model
    def _wip_period_bounds(self, period, date_from=None, date_to=None):
        """``(start_date, end_date)`` for a period key, either bound possibly
        None. Weeks run Monday-Sunday (Odoo's ISO-8601 convention)."""
        today = fields.Date.context_today(self)
        if period == "this_week":
            return start_of(today, "week"), end_of(today, "week")
        if period == "last_week":
            ref = today - relativedelta(weeks=1)
            return start_of(ref, "week"), end_of(ref, "week")
        if period == "this_month":
            return start_of(today, "month"), end_of(today, "month")
        if period == "last_month":
            ref = today - relativedelta(months=1)
            return start_of(ref, "month"), end_of(ref, "month")
        if period == "custom":
            return (
                fields.Date.to_date(date_from) if date_from else None,
                fields.Date.to_date(date_to) if date_to else None,
            )
        return None, None

    @api.model
    def _wip_date_domain(self, date_field, start, end):
        """Domain bounding ``date_field`` to ``[start, end]``.

        The bounds are whole days in the *user's* timezone converted to UTC, so
        "this week" means their week rather than the server's.
        """
        if not start and not end:
            return []
        tz = timezone(self.env.user.tz or "UTC")

        def to_utc(day, end_of_day):
            local = datetime.combine(day, time.max if end_of_day else time.min)
            utc_dt = tz.localize(local).astimezone(UTC).replace(tzinfo=None)
            # Serialised here rather than left as a datetime: this domain is
            # handed to the client and comes back on a tile click, so it has to
            # survive a JSON round trip.
            return fields.Datetime.to_string(utc_dt)

        domain = []
        if start:
            domain.append((date_field, ">=", to_utc(start, False)))
        if end:
            domain.append((date_field, "<=", to_utc(end, True)))
        return domain

    @api.model
    def _wip_filter_label(self, period, date_field, start, end):
        """Plain-language summary of what the numbers cover, shown in the
        header so a filtered total is never mistaken for the whole board."""
        field_label = _(dict(WIP_DATE_FIELDS)[date_field])
        if not start and not end:
            return _("%s · all time") % field_label
        shown = "%s → %s" % (
            format_date(self.env, start) if start else _("any"),
            format_date(self.env, end) if end else _("any"),
        )
        return "%s · %s" % (field_label, shown)

    # ------------------------------------------------------------------
    # Dashboard payload
    # ------------------------------------------------------------------
    @api.model
    def retrieve_unico_wip_dashboard(self, period="all", date_field="expected_date",
                                     date_from=None, date_to=None):
        """Tiles + drill-down domains for the Owl WIP dashboard.

        Every tile carries the domain that produced its count — including the
        period filter — so opening a tile shows exactly the orders it counted;
        board and tiles cannot disagree.
        """
        # Both selectors come from the client and end up in a domain, so they
        # are matched against the whitelists rather than trusted.
        valid_fields = dict(WIP_DATE_FIELDS)
        if date_field not in valid_fields:
            date_field = "expected_date"
        if period not in dict(WIP_PERIODS):
            period = "all"

        start, end = self._wip_period_bounds(period, date_from, date_to)
        base = ([("stage_id.is_closed", "=", False)]
                + self._wip_date_domain(date_field, start, end))
        totals = self._totals(base)
        return {
            "generated_at": fields.Datetime.to_string(fields.Datetime.now()),
            "currency": self._currency_payload(),
            "base_domain": base,
            "filters": {
                "period": period,
                "date_field": date_field,
                "date_from": fields.Date.to_string(start) if start else "",
                "date_to": fields.Date.to_string(end) if end else "",
                "label": self._wip_filter_label(period, date_field, start, end),
            },
            "period_options": [{"key": k, "label": _(l)} for k, l in WIP_PERIODS],
            "date_field_options": [
                {"key": k, "label": _(l)} for k, l in WIP_DATE_FIELDS],
            "total": totals["count"],
            "total_value": totals["value"],
            "total_cost": totals["cost"],
            "total_margin": totals["margin"],
            "margin_percent": totals["margin_percent"],
            "stages": self._stage_tiles(base),
            "urgency": self._urgency_tiles(base),
            "payment": self._payment_tiles(base),
            "blocked": self._blocked_tile(base),
            "pinned": self._pinned_tile(base),
            "watchlist": self._watchlist(base),
        }

    @api.model
    def _currency_payload(self):
        currency = self.env.company.currency_id
        return {
            "symbol": currency.symbol,
            "position": currency.position,
            "decimal_places": currency.decimal_places,
        }

    @api.model
    def _totals(self, domain):
        groups = self._read_group(
            domain,
            aggregates=["__count", "amount_untaxed:sum",
                        "estimated_cost:sum", "margin:sum"],
        )
        count, value, cost, margin = groups[0] if groups else (0, 0.0, 0.0, 0.0)
        return {
            "count": count,
            "value": value or 0.0,
            "cost": cost or 0.0,
            "margin": margin or 0.0,
            "margin_percent": (margin / value * 100.0) if value else 0.0,
        }

    @api.model
    def _tile(self, key, label, domain):
        totals = self._totals(domain)
        return {
            "key": key,
            "label": label,
            "res_model": "unico.wip.item",
            "domain": domain,
            "count": totals["count"],
            "value": totals["value"],
        }

    @api.model
    def _stage_tiles(self, base):
        # Driven by the stage records themselves, so a stage added in the UI
        # appears on the dashboard without a code change.
        stages = self.env["unico.wip.stage"].search([("is_closed", "=", False)])
        tiles = []
        for stage in stages:
            tile = self._tile(str(stage.id), stage.name,
                              base + [("stage_id", "=", stage.id)])
            # The colour hint travels with the tile: stage keys are record ids
            # now, so the client cannot infer meaning from the key alone.
            tile["token"] = stage.auto_token or ""
            tiles.append(tile)
        return tiles

    @api.model
    def _urgency_tiles(self, base):
        meta = [
            ("overdue", _("Overdue")),
            ("at_risk", _("At Risk (<= %s days)") % WIP_AT_RISK_WINDOW_DAYS),
            ("on_track", _("On Track")),
        ]
        return [
            self._tile(key, label, base + [("urgency", "=", key)])
            for key, label in meta
        ]

    @api.model
    def _payment_tiles(self, base):
        """Cash exposure of the board: how much of the WIP value is unpaid."""
        selection = dict(
            self._fields["payment_status"]._description_selection(self.env))
        return [
            self._tile(key, selection[key],
                       base + [("payment_status", "=", key)])
            for key in ("not_paid", "paid", "returned_credited")
            if key in selection
        ]

    @api.model
    def _blocked_tile(self, base):
        return self._tile("blocked", _("Flagged / Blocked"),
                          base + [("blocked", "=", True)])

    @api.model
    def _pinned_tile(self, base):
        return self._tile(
            "pinned", _("Pinned Off Detected Stage"),
            base + [("stage_override", "=", True)])

    @api.model
    def _watchlist(self, base):
        """The orders that need chasing: overdue or explicitly flagged."""
        domain = fields.Domain.AND([
            base,
            fields.Domain.OR([
                [("urgency", "=", "overdue")],
                [("blocked", "=", True)],
            ]),
        ])
        items = self.search(domain, limit=WIP_WATCHLIST_LIMIT)
        return {
            # Serialised to list form: the payload crosses to JS as JSON, and a
            # Domain object is not JSON-serialisable.
            "domain": list(domain),
            "total": self.search_count(domain),
            "shown": len(items),
            "rows": [{
                "id": item.id,
                "order_id": item.order_id.id,
                "name": item.order_id.name,
                "partner": item.partner_id.display_name,
                "stage": item.stage_id.name or "",
                "value": item.amount_untaxed,
                "expected_date": item.expected_date or False,
                "delay_days": item.delay_days,
                "blocked": item.blocked,
                "note": item.blocker_note or "",
                "supplier": item.supplier_names or "",
                "po_numbers": item.po_numbers or "",
                "owner": item.owner_id.display_name or "",
            } for item in items],
        }
