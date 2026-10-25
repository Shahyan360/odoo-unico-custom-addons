from collections import defaultdict

from odoo import _, api, fields, models

# Outgoing demand moves that still represent a pending need on the orderpoint.
# 'done' moves are already delivered (the demand is satisfied) so they no longer
# point at a live sale; everything else (incl. 'cancel') is kept so a cancelled
# sale can still surface its status instead of silently dropping to "N/A".
PENDING_DEMAND_MOVE_STATES = (
    "draft", "waiting", "confirmed", "assigned", "partially_available", "cancel",
)


class StockWarehouseOrderpoint(models.Model):
    _inherit = "stock.warehouse.orderpoint"

    unico_sale_order_status = fields.Char(
        string="Sale Order Status",
        compute="_compute_unico_sale_order_status",
        compute_sudo=True,
        help="Status of the sales order(s) driving this replenishment. A "
             "reordering rule has no direct link to a sale, so the demand is "
             "traced through the product's outgoing stock moves leaving this "
             "location (matched by product + location, like the deadline date), "
             "and those moves' sale order(s). Shows the single sale's status "
             "(Quotation / Sales Order / Cancelled), 'Sales Order' when several "
             "sales are linked and at least one is confirmed, 'Multiple SOs' "
             "otherwise, and 'N/A' when no sale is linked.",
    )
    unico_source_sale_order = fields.Char(
        string="Source Sale Order",
        compute="_compute_unico_source_sale_order",
        compute_sudo=True,
        help="Sale order number(s) driving this replenishment, traced through the "
             "product's outgoing stock moves and their sale line(s). Multiple "
             "orders are shown comma-separated; blank when no sale is linked.",
    )
    unico_source_purchase_note = fields.Text(
        string="Source Purchase Note",
        compute="_compute_unico_source_purchase_note",
        compute_sudo=True,
        help="Purchase Note(s) captured on the originating sale order line(s) this "
             "replenishment traces back to, so the sourcing instructions stay "
             "visible on the reordering rule. Blank when not traceable or no note "
             "was set.",
    )

    # ------------------------------------------------------------------
    # Sale order(s) behind a replenishment line. The orderpoint demand comes
    # from outgoing stock moves of the same product leaving the same location;
    # those moves carry ``sale_line_id`` when they originate from a sale. We
    # match moves to orderpoints by (product, location) exactly as core's
    # ``_compute_deadline_date`` does, then collect the sale line(s) behind them.
    # ------------------------------------------------------------------
    def _unico_replenishment_sale_lines(self):
        """Map each orderpoint id -> the sale.order.line recordset driving its
        demand (company-filtered). Everything else (sale orders, statuses,
        purchase notes) is derived from this single move lookup."""
        SaleLine = self.env["sale.order.line"].sudo()
        result = {op.id: SaleLine for op in self}
        pairs = {(op.product_id.id, op.location_id.id) for op in self if op.product_id and op.location_id}
        if not pairs:
            return result

        moves = self.env["stock.move"].sudo().with_context(active_test=False).search([
            ("product_id", "in", list({p for p, _loc in pairs})),
            ("location_id", "in", list({loc for _p, loc in pairs})),
            ("state", "in", PENDING_DEMAND_MOVE_STATES),
            ("sale_line_id", "!=", False),
        ])
        lines_by_pair = defaultdict(lambda: SaleLine)
        for move in moves:
            key = (move.product_id.id, move.location_id.id)
            if key in pairs:
                lines_by_pair[key] |= move.sale_line_id

        for op in self:
            lines = lines_by_pair.get((op.product_id.id, op.location_id.id), SaleLine)
            result[op.id] = lines.filtered(lambda l: l.company_id == op.company_id)
        return result

    def _unico_replenishment_sale_orders(self):
        """Map each orderpoint id -> the sale.order recordset driving its demand."""
        return {
            op_id: lines.order_id
            for op_id, lines in self._unico_replenishment_sale_lines().items()
        }

    # ------------------------------------------------------------------
    # Per-source RFQ grouping key (consumed by stock.rule).
    # ------------------------------------------------------------------
    # Enterprise drops ``orderpoint_id`` from the procurement values of *manual*
    # reordering rules (the ones the Replenishment "Order" button procures), so
    # stock.rule cannot recover the source from the values alone. We stamp a
    # stable key here so grouping works for both auto and manual reorders. An
    # orderpoint aggregates stock demand and cannot reliably allocate its buy to
    # one sale when several sales need the same product, so its key must remain
    # tied to the rule. Direct MTO procurements are keyed per sale in stock.rule.
    def _unico_orderpoint_group_key(self):
        self.ensure_one()
        return "op:%s" % self.id

    def _prepare_procurement_values(self, date=False):
        values = super()._prepare_procurement_values(date=date)
        values.setdefault("unico_source_group_key", self._unico_orderpoint_group_key())
        return values

    @api.depends("product_id", "location_id")
    def _compute_unico_source_sale_order(self):
        lines_map = self._unico_replenishment_sale_lines()
        SaleLine = self.env["sale.order.line"].sudo()
        for op in self:
            orders = lines_map.get(op.id, SaleLine).order_id
            op.unico_source_sale_order = ", ".join(sorted(orders.mapped("name"))) or False

    @api.depends("product_id", "location_id")
    def _compute_unico_source_purchase_note(self):
        SaleLine = self.env["sale.order.line"].sudo()
        # unico_purchase_note lives in unico_delivery; this module depends on core
        # only, so the field may be absent when installed standalone.
        has_note = "unico_purchase_note" in SaleLine._fields
        lines_map = self._unico_replenishment_sale_lines() if has_note else {}
        for op in self:
            notes = []
            for line in lines_map.get(op.id, SaleLine):
                note = (line.unico_purchase_note or "").strip()
                if note and note not in notes:
                    notes.append(note)
            op.unico_source_purchase_note = "\n".join(notes) or False

    @api.depends("product_id", "location_id")
    def _compute_unico_sale_order_status(self):
        sales_map = self._unico_replenishment_sale_orders()
        SaleOrder = self.env["sale.order"].sudo()
        state_labels = dict(SaleOrder._fields["state"]._description_selection(self.env))
        for op in self:
            sales = sales_map.get(op.id, SaleOrder)
            if not sales:
                op.unico_sale_order_status = "N/A"
            elif len(sales) == 1:
                op.unico_sale_order_status = state_labels.get(sales.state, sales.state)
            elif "sale" in sales.mapped("state"):
                # Several sales linked but at least one is a confirmed order:
                # surface it as a real Sales Order demand rather than burying it.
                op.unico_sale_order_status = state_labels.get("sale", "Sales Order")
            else:
                op.unico_sale_order_status = _("Multiple SOs")
