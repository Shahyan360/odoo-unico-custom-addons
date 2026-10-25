# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models

# Purchase states, by what they mean for the customer order.
#   RFQ    - still with the supplier, no agreed order yet
#   CLOSED - cancelled, so it no longer represents work in progress
#            ('done' is deliberately NOT closed here: a locked PO is an agreed
#            order like any other, and once its goods arrive it is exactly what
#            puts the sale on the Goods Received rung)
RFQ_PURCHASE_STATES = ("draft", "sent", "to approve")
CLOSED_PURCHASE_STATES = ("cancel",)

# Outbound progress that counts as "on its way to the customer".
IN_DELIVERY_STATUSES = ("started", "partial")

# Purchase origins name their sale(s) as "S00123" or "S00123 / 4", comma- or
# space-separated. Shared shape with unico_purchase's own origin parsing.
_ORIGIN_SPLIT = re.compile(r"[,\s]+")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # The WIP tracking itself lives on unico.wip.item — this is only the
    # backlink, so the order form can reach its tracker and nothing else about
    # work-in-progress management leaks onto the sales model.
    unico_wip_item_ids = fields.One2many(
        "unico.wip.item",
        "order_id",
        string="WIP Tracker",
    )

    # ------------------------------------------------------------------
    # Lifecycle detection — the rules the tracker's stage compute consumes.
    # These are all read-only helpers: no stored WIP state lives here.
    # ------------------------------------------------------------------
    def _unico_wip_is_open(self):
        """An open customer order: confirmed, and not yet fully delivered."""
        self.ensure_one()
        return self.state == "sale" and self.delivery_status != "full"

    def _unico_wip_open_purchases(self, purchases=None):
        """The still-open purchases among ``purchases`` (cancelled dropped)."""
        self.ensure_one()
        if purchases is None:
            purchases = self._unico_wip_purchase_map().get(self.id)
        if not purchases:
            return self.env["purchase.order"]
        return purchases.filtered(lambda p: p.state not in CLOSED_PURCHASE_STATES)

    def _unico_wip_stage_token(self, purchases=None):
        """The earliest rung of the lifecycle ladder that still has open work.

        An order is placed on its *bottleneck*, not the furthest it has got, so
        one unanswered RFQ reads "purchasing" even if part of it already shipped.
        """
        self.ensure_one()
        open_purchases = self._unico_wip_open_purchases(purchases)
        if any(p.state in RFQ_PURCHASE_STATES for p in open_purchases):
            return "purchasing"
        # An agreed order (confirmed or locked) whose goods have not all arrived.
        if any(
            p.state not in RFQ_PURCHASE_STATES and p.receipt_status != "full"
            for p in open_purchases
        ):
            return "shipping"
        if self.delivery_status in IN_DELIVERY_STATUSES:
            return "delivery"
        if open_purchases:
            # Everything ordered has arrived, but nothing has gone out yet.
            return "receipt"
        return "confirmed"

    def _unico_wip_days_to_due(self, due, today=None):
        """Days from ``today`` to ``due`` (negative = late). None when undated."""
        self.ensure_one()
        if not due:
            return None
        if today is None:
            today = fields.Date.context_today(self)
        due_date = fields.Datetime.context_timestamp(
            self, fields.Datetime.to_datetime(due)
        ).date()
        return (due_date - today).days

    # ------------------------------------------------------------------
    # Sale <-> purchase resolution
    # ------------------------------------------------------------------
    def _unico_wip_purchase_map(self):
        """``{order.id: purchase.order recordset}`` for the whole recordset.

        Two independent links, unioned:

        * ``sale.order.line.cpq_special_purchase_ids`` — the stored M2m that
          klouded_cpq stamps on special-order component purchases. Authoritative,
          and gives ``@api.depends`` a live path.
        * ``purchase.order.origin`` naming the sale — how unico_purchase's
          non-stock RFQs and the MTO / replenishment flows link back.

        Resolved in a single search for the whole batch (unlike unico_purchase's
        per-order ``_unico_related_purchase_orders``, which costs a query each),
        and matched on exact origin *tokens* rather than a bare substring so
        ``S00123`` never picks up ``S001234``.
        """
        PurchaseOrder = self.env["purchase.order"].sudo()
        result = {order.id: PurchaseOrder.browse() for order in self}
        if not self:
            return result

        # 1. CPQ special-order component purchases (stored relation).
        if "cpq_special_purchase_ids" in self.env["sale.order.line"]._fields:
            for order in self:
                purchases = order.order_line.sudo().cpq_special_purchase_ids
                if purchases:
                    result[order.id] |= purchases

        # 2. Origin-matched purchases, one search for the batch.
        names = {
            order.name: order.id
            for order in self
            if order.name and order.name != _("New")
        }
        if not names:
            return result
        domain = fields.Domain.AND([
            [("company_id", "in", self.company_id.ids)],
            fields.Domain.OR([[("origin", "ilike", name)] for name in names]),
        ])
        for purchase in PurchaseOrder.search(domain):
            for token in purchase._unico_wip_origin_tokens():
                order_id = names.get(token)
                if order_id:
                    result[order_id] |= purchase
        return result

    # ------------------------------------------------------------------
    # Tracker lifecycle
    # ------------------------------------------------------------------
    def _unico_ensure_wip_items(self):
        """Make sure every open order in ``self`` has a live tracker."""
        Item = self.env["unico.wip.item"].sudo()
        open_orders = self.filtered(lambda o: o._unico_wip_is_open())
        if not open_orders:
            return Item
        existing = Item.with_context(active_test=False).search(
            [("order_id", "in", open_orders.ids)])
        by_order = {item.order_id.id: item for item in existing}
        revived = existing.filtered(lambda i: not i.active)
        if revived:
            revived.active = True
        missing = [
            {"order_id": order.id}
            for order in open_orders
            if order.id not in by_order
        ]
        return existing | (Item.create(missing) if missing else Item)

    @api.model
    def _unico_sync_wip_items(self):
        """Reconcile the whole board: a tracker for every open order, and the
        finished / cancelled ones archived out of the way."""
        Item = self.env["unico.wip.item"].sudo()
        open_orders = self.sudo().search([
            ("state", "=", "sale"),
            ("delivery_status", "!=", "full"),
        ])
        open_orders._unico_ensure_wip_items()
        stale = Item.search([("order_id", "not in", open_orders.ids)])
        if stale:
            stale.active = False
        return True

    def action_confirm(self):
        """Open the tracker as soon as the customer confirms — the board should
        never lag behind the sale."""
        res = super().action_confirm()
        self._unico_ensure_wip_items()
        return res

    def action_unico_view_wip_item(self):
        self.ensure_one()
        item = self.unico_wip_item_ids[:1]
        return {
            "type": "ir.actions.act_window",
            "name": _("WIP Tracker"),
            "res_model": "unico.wip.item",
            "res_id": item.id,
            "view_mode": "form",
        }


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def _unico_wip_origin_tokens(self):
        """Bare sale-order names referenced by this purchase's ``origin``.

        Origins look like ``S00123``, ``S00123 / 4`` or a comma-separated run of
        both; the leading path segment of each part is the sale name.
        """
        self.ensure_one()
        tokens = set()
        for part in _ORIGIN_SPLIT.split(self.origin or ""):
            token = part.split("/")[0].strip()
            if token:
                tokens.add(token)
        return tokens
