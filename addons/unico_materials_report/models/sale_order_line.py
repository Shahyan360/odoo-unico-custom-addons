# -*- coding: utf-8 -*-
from odoo import api, fields, models

# The waiting categories, in display order. Shared with sale_order.py. A line can
# carry MORE THAN ONE (e.g. a special door whose components are both on an
# unreceived purchase AND not yet reserved for its manufacturing order).
#   stock         - a plain stockable line short on finished-goods stock
#   purchase      - a special-order line waiting on an incoming purchase (RFQ/PO)
#   manufacturing - a configured/special door whose MO (and its SFP / pick-
#                   components transfer) is waiting on component availability
UNICO_MATERIAL_REASONS = [
    ("stock", "Awaiting Stock"),
    ("purchase", "Awaiting Purchase"),
    ("manufacturing", "Awaiting Manufacturing"),
]

# A manufacturing order is "waiting on components" when it is still open and its
# raw materials are not fully reserved. mrp.production.reservation_state is
# 'assigned' when material is available; 'confirmed'/'waiting' otherwise.
CLOSED_MO_STATES = ("done", "cancel")
NOT_READY_MO_RESERVATION = ("confirmed", "waiting")


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    # Stored inverse of mrp.production.sale_line_id (defined in sale_mrp). Unlike
    # the CPQ cpq_production_ids (computed-by-search, so no dependency path), this
    # real relation lets the wait compute below stay live via @api.depends when a
    # linked MO's reservation_state flips.
    unico_mo_ids = fields.One2many(
        "mrp.production",
        "sale_line_id",
        string="Manufacturing Orders (Material Report)",
        help="Manufacturing orders sourced from this line; used to detect when a "
             "configured/special door is waiting on component availability.",
    )
    unico_material_waiting = fields.Boolean(
        string="Waiting on Materials",
        compute="_compute_unico_material_wait",
        store=True,
        index=True,
    )
    unico_material_wait_reasons = fields.Char(
        string="Wait Reason Tokens",
        compute="_compute_unico_material_wait",
        store=True,
        index=True,
        help="Comma-joined machine tokens of what this line is waiting on (a line "
             "can wait on several at once): stock, purchase, manufacturing.",
    )
    unico_material_wait_label = fields.Char(
        string="Waiting On",
        compute="_compute_unico_material_wait",
        store=True,
    )

    @api.depends(
        "order_id.state",
        "display_type",
        "product_id",
        "product_uom_qty",
        "is_configurable",
        "is_special_order_product",
        # normal-product stock signal (unico_delivery); best-effort here, the
        # cron re-evaluates for live stock drift that has no dependency path.
        "unico_stock_shortfall",
        # manufacturing — stored relation, so this stays live.
        "unico_mo_ids.reservation_state",
        "unico_mo_ids.state",
        # special-order purchases — stored M2m, so this stays live.
        "cpq_special_purchase_ids.state",
        "cpq_special_purchase_ids.receipt_status",
    )
    def _compute_unico_material_wait(self):
        labels = dict(UNICO_MATERIAL_REASONS)
        for line in self:
            tokens = line._unico_material_wait_tokens()
            line.unico_material_wait_reasons = ",".join(tokens)
            line.unico_material_wait_label = " + ".join(labels[token] for token in tokens)
            line.unico_material_waiting = bool(tokens)

    def _unico_material_wait_tokens(self):
        """All waiting reasons that apply to this line (possibly several)."""
        self.ensure_one()
        order = self.order_id
        if order.state != "sale":
            return []
        if self.display_type or not self.product_id or self.product_uom_qty <= 0:
            return []
        if self.product_id.type == "service":
            return []
        tokens = []
        if self.is_configurable or self.is_special_order_product:
            # A door can be blocked on its incoming special-component purchase
            # AND on its manufacturing-order component reservation at the same
            # time — surface both rather than collapsing to one.
            if self._unico_special_purchase_waiting():
                tokens.append("purchase")
            if self._unico_door_components_waiting():
                tokens.append("manufacturing")
        elif self.unico_stock_shortfall:
            tokens.append("stock")
        return tokens

    def _unico_door_components_waiting(self):
        self.ensure_one()
        return any(
            mo.state not in CLOSED_MO_STATES
            and mo.reservation_state in NOT_READY_MO_RESERVATION
            for mo in self.unico_mo_ids
        )

    def _unico_special_purchase_waiting(self):
        self.ensure_one()
        if "cpq_special_purchase_ids" not in self._fields:
            return False
        purchases = self.sudo().cpq_special_purchase_ids
        # Open purchase (RFQ or confirmed PO) whose goods have not fully arrived:
        # receipt_status is False on a draft RFQ and 'pending'/'partial' until the
        # receipt is validated ('full').
        return any(
            po.state not in ("done", "cancel") and po.receipt_status != "full"
            for po in purchases
        )

    def _unico_refresh_material_wait(self):
        """Force a recompute + store of the line wait fields (used by the cron to
        pick up live stock drift that has no dependency path)."""
        if not self:
            return
        fields_to_refresh = [
            "unico_material_waiting",
            "unico_material_wait_reasons",
            "unico_material_wait_label",
        ]
        self.invalidate_recordset(fields_to_refresh)
        for fname in fields_to_refresh:
            self.env.add_to_compute(self._fields[fname], self)
        self.flush_recordset(fields_to_refresh)
