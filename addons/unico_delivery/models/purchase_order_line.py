import re

from odoo import api, fields, models

# Sale states whose sourcing notes are still relevant to a buyer. Quotations
# (draft/sent) are included because Min/Max replenishment is often raised to
# cover demand that is still on a quotation — those are exactly the orders the
# salesperson left instructions on.
RELEVANT_SALE_STATES = ("draft", "sent", "sale")

# A replenishment PO's origin carries the orderpoint references that triggered
# it (e.g. "Replenishment Report, OP/00088, OP/00055"), never a sale order name.
# Either signal marks a line as replenishment-sourced rather than sale-sourced.
_ORDERPOINT_REF_RE = re.compile(r"OP/\d+")


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    unico_cpq_special_sale_line_id = fields.Many2one(
        "sale.order.line",
        string="CPQ Special Sale Line",
        copy=False,
        index=True,
        ondelete="set null",
        help="Marks purchase lines generated from CPQ special-order components so RFQ refreshes can update/remove only those lines and preserve buyer-added lines.",
    )

    # Mirrors the sale.order.line "Purchase Note" (sourcing instructions sales
    # wrote for a short-stock / special-order line) onto the purchase line that
    # was raised to fulfil it, so purchasing sees the same guidance when deciding
    # what to buy.
    unico_purchase_note = fields.Text(
        string="Purchase Note",
        compute="_compute_unico_purchase_note",
        help="Sourcing / purchasing instructions captured on the originating "
             "sale order line, shown here to inform the purchase decision.",
    )

    # CPQ configuration details (door dimensions or custom-product category,
    # description, vendor SKU, and quote references) for special-order lines.
    unico_purchase_cpq_note = fields.Text(
        string="CPQ Configuration Details",
        compute="_compute_unico_purchase_cpq_note",
        help="Configuration, vendor SKU, and quote details from the originating CPQ special-order line.",
    )

    @api.depends("product_id", "order_id.origin")
    def _compute_unico_purchase_cpq_note(self):
        Component = self.env["sale.order.line.cpq.special.component"].sudo()
        note_by_line = {}
        for component in Component.search([("purchase_line_id", "in", self.ids)]):
            sale_line = component.sale_line_id
            pline_id = component.purchase_line_id.id
            if not sale_line or pline_id in note_by_line:
                continue
            note = sale_line._cpq_special_purchase_note(component)
            if note:
                note_by_line[pline_id] = note
        for line in self:
            line.unico_purchase_cpq_note = note_by_line.get(line.id, False)

    @api.depends("product_id", "order_id.origin")
    def _compute_unico_purchase_note(self):
        Component = self.env["sale.order.line.cpq.special.component"].sudo()
        SaleLine = self.env["sale.order.line"].sudo()

        # 1. CPQ special-order link: component -> sale line (single query).
        note_by_line = {}
        components = Component.search([("purchase_line_id", "in", self.ids)])
        for component in components:
            sale_note = component.sale_line_id.unico_purchase_note
            if sale_note and component.purchase_line_id.id not in note_by_line:
                note_by_line[component.purchase_line_id.id] = sale_note

        # 2. Plain non-stock RFQ: match the sale by name-in-origin + product.
        plain = self.filtered(
            lambda line: line.id not in note_by_line
            and line.order_id.origin
            and line.product_id
        )
        if plain:
            candidate_lines = SaleLine.search([
                ("product_id", "in", plain.product_id.ids),
                ("unico_purchase_note", "!=", False),
            ])
            for line in plain:
                origin = line.order_id.origin or ""
                notes = []
                for sale_line in candidate_lines:
                    order_name = sale_line.order_id.name
                    if (
                        sale_line.product_id == line.product_id
                        and order_name
                        and order_name in origin
                        and sale_line.unico_purchase_note not in notes
                    ):
                        notes.append(sale_line.unico_purchase_note)
                if notes:
                    note_by_line[line.id] = "\n".join(notes)

        # 3. Min/Max replenishment PO: there is no sale name in the origin to
        #    match on (it carries orderpoint refs, see _is_replenishment_origin),
        #    and a single replenishment line aggregates demand from many open
        #    sales. Surface every open sale's note for the product, attributed to
        #    the order + salesperson so the buyer knows who asked and why — the
        #    information that was previously lost on replenishment POs.
        replenishment = self.filtered(
            lambda line: line.id not in note_by_line
            and line.product_id
            and self._is_replenishment_origin(line.order_id.origin)
        )
        if replenishment:
            demand_lines = SaleLine.search([
                ("product_id", "in", replenishment.product_id.ids),
                ("unico_purchase_note", "!=", False),
                ("order_id.state", "in", list(RELEVANT_SALE_STATES)),
            ])
            demand_by_product = {}
            for sale_line in demand_lines:
                demand_by_product.setdefault(
                    sale_line.product_id.id, SaleLine
                )
                demand_by_product[sale_line.product_id.id] |= sale_line
            for line in replenishment:
                entry = self._format_replenishment_note(
                    demand_by_product.get(line.product_id.id)
                )
                if entry:
                    note_by_line[line.id] = entry

        for line in self:
            line.unico_purchase_note = note_by_line.get(line.id, False)

    @staticmethod
    def _is_replenishment_origin(origin):
        """True when the origin looks like it was raised by Min/Max replenishment
        (orderpoint references) rather than for a specific sale order."""
        if not origin:
            return False
        return bool(_ORDERPOINT_REF_RE.search(origin)) or "Replenishment" in origin

    @api.model
    def _format_replenishment_note(self, sale_lines):
        """Group a product's open-sale notes for display on a replenishment line.

        One block per distinct note text, each followed by the orders (and their
        salespeople) that asked for it, so a buyer can see both the instruction
        and who requested it."""
        if not sale_lines:
            return False
        orders_by_note = {}
        for sale_line in sale_lines:
            note = (sale_line.unico_purchase_note or "").strip()
            if not note:
                continue
            order = sale_line.order_id
            salesperson = order.user_id.name
            label = "%s (%s)" % (order.name, salesperson) if salesperson else order.name
            requesters = orders_by_note.setdefault(note, [])
            if label not in requesters:
                requesters.append(label)
        blocks = []
        for note, requesters in orders_by_note.items():
            requester_label = self.env._("Requested by: %s", ", ".join(requesters))
            blocks.append("%s\n%s" % (note, requester_label))
        return "\n\n".join(blocks) if blocks else False
