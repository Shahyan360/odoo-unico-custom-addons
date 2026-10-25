from odoo import models

# Quantities on a delivery are floats; compare them with a tolerance rather
# than exactly so 3.0000000001 still counts as three whole units.
UNIT_SPLIT_EPSILON = 1e-6


class StockMove(models.Model):
    _inherit = "stock.move"

    # ------------------------------------------------------------------
    # Delivery slip description
    # ------------------------------------------------------------------
    def _unico_delivery_slip_description(self):
        """Text printed under the product on the delivery slip.

        Odoo only prints ``description_picking``, which is built from the
        product (its picking description plus the variant values) and never
        carries what the salesperson typed under the product on the
        quotation. The customer reads the delivery slip, so it has to say the
        same thing the order confirmation said: the sale line description
        and, for configured products, the CPQ attribute breakdown.
        """
        self.ensure_one()
        # sudo: inventory-only users print delivery slips but cannot read
        # sale.order.line; the text is display-only.
        sale_line = self.sudo().sale_line_id
        blocks = [self.description_picking or ""]
        if sale_line:
            blocks.append(sale_line.name or "")
            if "cpq_configuration_description" in sale_line._fields:
                blocks.append(sale_line.cpq_configuration_description or "")
        return self._unico_merge_description_blocks(blocks, self.product_id)

    @staticmethod
    def _unico_merge_description_blocks(blocks, product):
        """Join description blocks, dropping the product name and repeats.

        The three sources overlap heavily — ``description_picking`` already
        starts with the variant values, and the sale line name repeats the
        product name — so the blocks are merged line by line and any line
        already printed (or that is just the product name again) is skipped.
        """
        product_names = {
            (name or "").strip().lower()
            for name in (product.display_name, product.name)
        }
        product_names.discard("")
        merged = []
        seen = set()
        for block in blocks:
            text = block or ""
            for candidate in (product.display_name, product.name):
                if candidate and text.startswith(candidate):
                    # CPQ names lines "<product> - <door style>", so drop the
                    # separator too and keep the style on its own line.
                    text = text[len(candidate):].lstrip(" \t-–—:,")
                    break
            for raw_line in text.splitlines():
                line = raw_line.strip()
                key = line.lower()
                if not line or key in product_names or key in seen:
                    continue
                seen.add(key)
                merged.append(line)
        return "\n".join(merged)

    # ------------------------------------------------------------------
    # One printed row per unique unit
    # ------------------------------------------------------------------
    def _unico_is_unique_unit_line(self):
        """Whether every unit of this move is its own distinct product.

        A configured (Configure Door / CPQ) line is built to order: three
        units are three individually made doors, not three interchangeable
        pieces of stock. They must therefore be listed one per row on the
        delivery slip so each can be checked off on its own.
        """
        self.ensure_one()
        if self.bom_line_id:
            # Kit component: it belongs to its parent kit's row, not its own.
            return False
        sale_line = self.sudo().sale_line_id
        if not sale_line:
            return False
        return bool(
            "is_configurable" in sale_line._fields and sale_line.is_configurable
        )

    @staticmethod
    def _unico_unit_split_count(qty_ordered, quantity):
        """How many one-per-unit rows a unique-unit line should print."""
        biggest = max(qty_ordered or 0.0, quantity or 0.0)
        count = int(biggest + UNIT_SPLIT_EPSILON)
        if abs(biggest - count) > UNIT_SPLIT_EPSILON:
            # A fractional quantity is not a countable set of unique units;
            # leave it on a single row rather than inventing partial ones.
            return 1
        return max(count, 1)

    @staticmethod
    def _unico_split_unit_quantities(qty_ordered, quantity, count):
        """Spread ordered/delivered quantities one unit at a time.

        Returns ``count`` ``(ordered, delivered)`` pairs. Short shipments stay
        readable: 3 ordered / 2 delivered prints as 1/1, 1/1, 1/0.
        """
        remaining = [qty_ordered or 0.0, quantity or 0.0]
        rows = []
        for _index in range(count):
            row = []
            for position, left in enumerate(remaining):
                taken = 1.0 if left > 1.0 + UNIT_SPLIT_EPSILON else max(left, 0.0)
                remaining[position] = left - taken
                row.append(taken)
            rows.append(tuple(row))
        return rows

    def _unico_delivery_slip_move_rows(self):
        """Rows for the not-yet-done delivery slip table (one dict per row)."""
        rows = []
        for move in self:
            description = move._unico_delivery_slip_description()
            qty_ordered = move.product_uom_qty
            quantity = move.quantity
            count = (
                move._unico_unit_split_count(qty_ordered, quantity)
                if move._unico_is_unique_unit_line()
                else 1
            )
            if count <= 1:
                rows.append({
                    "move": move,
                    "description": description,
                    "product_uom_qty": qty_ordered,
                    "quantity": quantity,
                })
                continue
            for row_ordered, row_quantity in move._unico_split_unit_quantities(
                qty_ordered, quantity, count
            ):
                rows.append({
                    "move": move,
                    "description": description,
                    "product_uom_qty": row_ordered,
                    "quantity": row_quantity,
                })
        return rows
