import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Purchase states that still represent an open, in-progress order (an RFQ that
# could still cover demand, or a confirmed PO whose goods have not all arrived).
OPEN_PURCHASE_STATES = ("draft", "sent", "purchase")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    unico_purchase_count = fields.Integer(
        string="Related Purchases",
        compute="_compute_unico_purchase_count",
        compute_sudo=True,
        help="Open RFQs / Purchase Orders auto-created for this sale order "
             "(matched by the sale's reference on the purchase origin).",
    )
    # UI-only guard: True for everyone who is NOT a Sales Manager, used to make
    unico_salesperson_readonly = fields.Boolean(
        string="Salesperson Locked",
        compute="_compute_unico_salesperson_readonly",
    )

    @api.depends_context("uid")
    def _compute_unico_salesperson_readonly(self):
        is_manager = self.env.user.has_group("sales_team.group_sale_manager")
        for order in self:
            order.unico_salesperson_readonly = not is_manager

    # ------------------------------------------------------------------
    # Feature 2 — purchases auto-created for this order
    # ------------------------------------------------------------------
    def _unico_related_purchase_orders(self):
        """Open purchases (RFQ/PO) auto-created for this sale order.

        Both special-order auto-RFQ flows stamp the sale's name onto the
        purchase ``origin`` (this module's non-stock RFQs use ``order.name``,
        and the klouded_cpq / unico_delivery special-component RFQs use
        ``"<order.name> / <line>"``), whereas Min/Max replenishment does not.
        Matching on origin therefore surfaces exactly the purchases raised for
        this order, instead of every open PO that happens to share a product.
        """
        self.ensure_one()
        if not self.name or self.name == _("New"):
            return self.env["purchase.order"].sudo()
        return self.env["purchase.order"].sudo().search([
            ("origin", "ilike", self.name),
            ("state", "in", OPEN_PURCHASE_STATES),
            ("company_id", "=", self.company_id.id),
        ])

    def _compute_unico_purchase_count(self):
        for order in self:
            order.unico_purchase_count = len(order._unico_related_purchase_orders())

    def action_unico_view_purchases(self):
        self.ensure_one()
        purchases = self._unico_related_purchase_orders()
        return {
            "type": "ir.actions.act_window",
            "name": _("Purchases for %s", self.name),
            "res_model": "purchase.order",
            "view_mode": "list,form",
            "domain": [("id", "in", purchases.ids)],
            "context": {"create": False},
        }

    # ------------------------------------------------------------------
    # Keep purchase urgency in sync with sale commitment dates
    # ------------------------------------------------------------------
    # purchase.order.unico_urgency is product-linked to sales, a relation Odoo's
    # @api.depends cannot track, so we push a recompute to the affected purchases
    # whenever a sale's commitment date, state or lines change.
    _UNICO_URGENCY_TRIGGER_FIELDS = frozenset({"commitment_date", "state", "order_line"})

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._unico_refresh_related_purchase_urgency()
        return orders

    def write(self, vals):
        res = super().write(vals)
        if self._UNICO_URGENCY_TRIGGER_FIELDS.intersection(vals):
            self._unico_refresh_related_purchase_urgency()
        return res

    def _unico_refresh_related_purchase_urgency(self):
        """Recompute sales-urgency on every open purchase that shares a product
        with these sales (so changing a commitment date is reflected at once).

        Runs with elevated rights: this is a background side effect of saving a
        sale, so a salesperson without Purchase access must still be able to
        create/edit/confirm their quote without hitting an AccessError."""
        products = self.order_line.product_id
        if not products:
            return
        purchases = self.env["purchase.order"].sudo().search([
            ("state", "in", ["draft", "sent", "to approve", "purchase"]),
            ("order_line.product_id", "in", products.ids),
            ("company_id", "in", self.company_id.ids),
        ])
        purchases._unico_recompute_urgency()

    # ------------------------------------------------------------------
    # Feature 1 — reordering on sale confirmation
    # ------------------------------------------------------------------
    # Odoo already replenishes on confirmation natively: confirming a sale
    # creates/“confirms” its delivery stock moves, and stock.move._trigger_scheduler
    # then procures the *auto* orderpoint **at that move's location** — but only
    # when the move drives the forecast below the rule's minimum. That is exactly
    # the desired behaviour (no RFQ while the product is in stock / above min), so
    # we deliberately do NOT run our own procurement here; doing so would
    # over-create RFQs by firing every orderpoint of the product regardless of
    # location or stock level.
    def action_confirm(self):
        res = super().action_confirm()
        self._unico_create_special_order_rfqs()
        self._unico_refresh_related_purchase_urgency()
        return res

    # ------------------------------------------------------------------
    # Special-order auto-purchasing (products with NO Min/Max rule)
    # ------------------------------------------------------------------
    # Min/Max products are replenished by Odoo natively (see action_confirm note).
    # Products *without* a rule that are short on stock — the "non-stock / special
    # order" items — get no automation out of the box, so here we auto-create an
    # RFQ for exactly the shortfall, picking the product's first vendor (or the
    # company default vendor from Purchase settings).
    def _unico_resolve_special_vendor(self, product):
        """First vendor on the product (lowest sequence), else the company
        default purchase vendor. Returns an empty recordset if neither exists."""
        seller = product.seller_ids.filtered(lambda s: s.partner_id)[:1]
        if seller:
            return seller.partner_id
        return self.company_id.unico_default_purchase_vendor_id

    def _unico_prepare_special_po_line(self, product, qty, vendor):
        seller = product.seller_ids.filtered(lambda s: s.partner_id == vendor)[:1]
        price = seller.price if seller else product.standard_price
        return {
            "product_id": product.id,
            "name": product.display_name,
            "product_qty": qty,
            "product_uom_id": product.uom_id.id,
            "price_unit": price,
            "date_planned": self.commitment_date or fields.Datetime.now(),
        }

    def _unico_create_special_order_rfqs(self):
        Orderpoint = self.env["stock.warehouse.orderpoint"].sudo()
        POLine = self.env["purchase.order.line"].sudo()
        Purchase = self.env["purchase.order"]
        for order in self:
            company = order.company_id
            lines_by_vendor = {}
            for line in order.order_line:
                product = line.product_id
                if line.display_type or not product or product.type == "service":
                    continue
                # CPQ configurable / special-order lines are sourced by the
                # klouded_cpq + unico_delivery special-order RFQ flow — leave them
                # to it so we never create a duplicate RFQ.
                if getattr(line, "is_configurable", False) or \
                        getattr(line, "is_special_order_product", False):
                    continue
                # Skip products that have a Min/Max rule — Odoo replenishes those.
                if Orderpoint.search_count([
                    ("product_id", "=", product.id),
                    ("company_id", "=", company.id),
                ]):
                    continue
                # Shortfall = ordered, not-yet-delivered, beyond free stock on hand.
                free = product.with_company(company).free_qty
                shortfall = line.product_uom_qty - line.qty_delivered - max(free, 0.0)
                shortfall = product.uom_id.round(shortfall)
                if shortfall <= 0:
                    continue
                # Skip if an open purchase already covers this sale + product
                # (a native MTO PO, or a previous run of this method — both carry
                # the sale's name in ``origin``), so we never double-order.
                if POLine.search_count([
                    ("product_id", "=", product.id),
                    ("order_id.state", "in", ("draft", "sent", "purchase")),
                    ("order_id.origin", "ilike", order.name),
                ]):
                    continue
                vendor = order._unico_resolve_special_vendor(product)
                if not vendor:
                    order._unico_alert_missing_vendor(product)
                    continue
                lines_by_vendor.setdefault(vendor, []).append((product, shortfall))
            for vendor, items in lines_by_vendor.items():
                vals = {
                    "partner_id": vendor.id,
                    "origin": order.name,
                    "company_id": company.id,
                    "order_line": [
                        (0, 0, order._unico_prepare_special_po_line(p, qty, vendor))
                        for (p, qty) in items
                    ],
                }
                # Auto-purchasing is a side effect of confirming the sale: it must
                # never block confirmation. Isolate each RFQ in a savepoint so a
                # failure (blocking vendor warning, pricelist/currency issue, a
                # constraint from another module, ...) rolls back only that RFQ
                # and surfaces as a chatter alert instead of an exception.
                try:
                    with self.env.cr.savepoint():
                        Purchase.create(vals)
                except Exception as exc:  # noqa: BLE001 - deliberately broad
                    _logger.warning(
                        "Auto RFQ for sale %s / vendor %s failed: %s",
                        order.name, vendor.display_name, exc,
                    )
                    order.message_post(body=_(
                        "Could not auto-create the RFQ for vendor %(vendor)s on "
                        "this special order (%(error)s). Please raise it manually.",
                        vendor=vendor.display_name, error=exc,
                    ))

    def _unico_alert_missing_vendor(self, product):
        """Alert purchasing that a special-order item needs a vendor (replaces the
        manual email): no vendor on the product and no company default set."""
        self.ensure_one()
        self.message_post(body=_(
            "Special-order product %s needs a vendor — none is set on the product "
            "and no Default Purchase Vendor is configured in Purchase settings. "
            "Please assign a vendor and raise the RFQ.",
            product.display_name,
        ))
