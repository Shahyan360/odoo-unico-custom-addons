# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

from .sale_order_line import UNICO_MATERIAL_REASONS

# Promised within this many days (and not past) counts as "urgent".
MATERIAL_URGENT_WINDOW_DAYS = 3

# SPF / pick-components transfers are identified purely by name (they carry
# "SFP"/"SPF"); "waiting" = not yet ready/done.
SPF_NOT_READY_PICKING_STATES = ("draft", "waiting", "confirmed")

# A manufacturing order is waiting on components when it is open and not fully
# reserved.
CLOSED_MO_STATES = ("done", "cancel")
NOT_READY_MO_RESERVATION = ("confirmed", "waiting")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    unico_waiting_on_materials = fields.Boolean(
        string="Waiting on Materials",
        compute="_compute_unico_material_status",
        store=True,
        index=True,
        help="A confirmed order with at least one line that cannot yet be "
             "fulfilled / enter production because it is waiting on materials.",
    )
    unico_material_blocking_reasons = fields.Char(
        string="Blocking Reason Tokens",
        compute="_compute_unico_material_status",
        store=True,
        help="Comma-joined machine tokens of the waiting categories present on "
             "this order (for filtering): stock, purchase, manufacturing.",
    )
    unico_material_blocking_label = fields.Char(
        string="Waiting On",
        compute="_compute_unico_material_status",
        store=True,
    )
    unico_waiting_line_count = fields.Integer(
        string="Waiting Lines",
        compute="_compute_unico_material_status",
        store=True,
    )
    unico_material_urgency = fields.Selection(
        selection=[
            ("overdue", "Overdue"),
            ("urgent", "Urgent"),
            ("scheduled", "Scheduled"),
            ("none", "Not Waiting"),
        ],
        string="Material Urgency",
        compute="_compute_unico_material_status",
        store=True,
        index=True,
        default="none",
        help="How time-critical the wait is, from the order's commitment date: "
             "overdue, due within %s days (urgent), later (scheduled), or not "
             "waiting." % MATERIAL_URGENT_WINDOW_DAYS,
    )

    @api.depends(
        "state",
        "order_line.unico_material_waiting",
        "order_line.unico_material_wait_reasons",
        "commitment_date",
    )
    def _compute_unico_material_status(self):
        today = fields.Date.context_today(self)
        labels = dict(UNICO_MATERIAL_REASONS)
        for order in self:
            reasons = []
            waiting_lines = order.order_line.filtered("unico_material_waiting")
            if order.state == "sale" and waiting_lines:
                present = set()
                for line in waiting_lines:
                    present |= set(filter(None, (line.unico_material_wait_reasons or "").split(",")))
                reasons = [token for token, _label in UNICO_MATERIAL_REASONS if token in present]
            order.unico_material_blocking_reasons = ",".join(reasons)
            order.unico_material_blocking_label = ", ".join(labels[token] for token in reasons)
            order.unico_waiting_line_count = len(waiting_lines) if order.state == "sale" else 0
            waiting = bool(reasons)
            order.unico_waiting_on_materials = waiting
            order.unico_material_urgency = (
                order._unico_material_urgency_value(today) if waiting else "none"
            )

    def _unico_material_urgency_value(self, today=None):
        self.ensure_one()
        if today is None:
            today = fields.Date.context_today(self)
        due = self.commitment_date or self.expected_date
        if not due:
            return "scheduled"
        due_date = fields.Datetime.context_timestamp(
            self, fields.Datetime.to_datetime(due)
        ).date()
        days = (due_date - today).days
        if days < 0:
            return "overdue"
        if days <= MATERIAL_URGENT_WINDOW_DAYS:
            return "urgent"
        return "scheduled"

    def _unico_refresh_material_status(self):
        if not self:
            return
        fields_to_refresh = [
            "unico_waiting_on_materials",
            "unico_material_blocking_reasons",
            "unico_material_blocking_label",
            "unico_waiting_line_count",
            "unico_material_urgency",
        ]
        self.invalidate_recordset(fields_to_refresh)
        for fname in fields_to_refresh:
            self.env.add_to_compute(self._fields[fname], self)
        self.flush_recordset(fields_to_refresh)

    @api.model
    def _cron_recompute_unico_material_status(self):
        """Re-evaluate confirmed orders: refresh line wait reasons (catches live
        stock drift for normal products, which has no dependency path) then the
        order rollup + time-relative urgency."""
        orders = self.search([("state", "=", "sale")])
        orders.order_line._unico_refresh_material_wait()
        orders._unico_refresh_material_status()

    @api.model
    def retrieve_unico_materials_dashboard(self):
        """Counts + drill-down domains for the Owl dashboard tiles. The order
        urgency/reason tile domains match the order search-view filters; the SPF
        and MO tiles drill into the resolved record ids."""
        today = fields.Date.context_today(self)
        base = [("unico_waiting_on_materials", "=", True)]
        urgency_meta = [
            ("overdue", _("Overdue")),
            ("urgent", _("Urgent (<= %s days)") % MATERIAL_URGENT_WINDOW_DAYS),
            ("scheduled", _("Scheduled")),
        ]
        urgency = []
        for key, label in urgency_meta:
            domain = base + [("unico_material_urgency", "=", key)]
            urgency.append({
                "key": key,
                "label": label,
                "res_model": "sale.order",
                "domain": domain,
                "count": self.search_count(domain),
            })
        reasons = []
        for key, label in UNICO_MATERIAL_REASONS:
            domain = base + [("unico_material_blocking_reasons", "ilike", key)]
            reasons.append({
                "key": key,
                "label": _(label),
                "res_model": "sale.order",
                "domain": domain,
                "count": self.search_count(domain),
            })
        return {
            "generated_at": fields.Datetime.to_string(fields.Datetime.now()),
            "total": self.search_count(base),
            "base_domain": base,
            "urgency": urgency,
            "reasons": reasons,
            "spf_pickings": self._unico_urgency_bucket_tiles(
                self._unico_spf_picking_buckets(today), "stock.picking"),
            "manufacturing_orders": self._unico_urgency_bucket_tiles(
                self._unico_mo_component_buckets(today), "mrp.production"),
        }

    def _unico_urgency_bucket(self, due, today=None):
        """Bucket a due datetime into overdue / urgent / scheduled."""
        if today is None:
            today = fields.Date.context_today(self)
        if not due:
            return "scheduled"
        due_date = fields.Datetime.context_timestamp(
            self, fields.Datetime.to_datetime(due)
        ).date()
        days = (due_date - today).days
        if days < 0:
            return "overdue"
        if days <= MATERIAL_URGENT_WINDOW_DAYS:
            return "urgent"
        return "scheduled"

    @api.model
    def _unico_urgency_bucket_tiles(self, buckets, res_model):
        meta = [
            ("overdue", _("Overdue")),
            ("urgent", _("Urgent (<= %s days)") % MATERIAL_URGENT_WINDOW_DAYS),
            ("scheduled", _("Scheduled")),
        ]
        tiles = []
        for key, label in meta:
            ids = buckets.get(key, [])
            tiles.append({
                "key": key,
                "label": label,
                "res_model": res_model,
                "domain": [("id", "in", ids)],
                "count": len(ids),
            })
        return tiles

    @api.model
    def _unico_spf_picking_buckets(self, today):
        """Open SPF/SFP-named pick-components transfers, bucketed by the urgency
        of the sale they feed (delivery date), falling back to the picking's own
        scheduled date when no sale can be resolved."""
        pickings = self.env["stock.picking"].sudo().search([
            ("state", "in", list(SPF_NOT_READY_PICKING_STATES)),
            "|", ("name", "ilike", "SFP"), ("name", "ilike", "SPF"),
        ])
        buckets = {"overdue": [], "urgent": [], "scheduled": []}
        for picking in pickings:
            order = picking.sale_id
            if not order:
                # Component pickings have no direct sale link; resolve via the MO
                # they feed (their moves' destinations are the MO raw moves).
                productions = picking.move_ids.move_dest_ids.raw_material_production_id
                order = productions.sale_line_id.order_id[:1]
            due = order.commitment_date or order.expected_date if order.state == "sale" else False
            due = due or picking.scheduled_date
            buckets[self._unico_urgency_bucket(due, today)].append(picking.id)
        return buckets

    @api.model
    def _unico_mo_component_buckets(self, today):
        """Manufacturing orders waiting on component availability, bucketed by
        the urgency of the sale they belong to (delivery date)."""
        productions = self.env["mrp.production"].sudo().search([
            ("state", "not in", list(CLOSED_MO_STATES)),
            ("reservation_state", "in", list(NOT_READY_MO_RESERVATION)),
        ])
        buckets = {"overdue": [], "urgent": [], "scheduled": []}
        for production in productions:
            order = production.sale_line_id.order_id
            due = order.commitment_date or order.expected_date if order.state == "sale" else False
            due = due or production.date_start
            buckets[self._unico_urgency_bucket(due, today)].append(production.id)
        return buckets
