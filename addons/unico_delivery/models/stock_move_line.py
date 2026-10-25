import hashlib

from odoo import models


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    def _get_aggregated_properties(self, move_line=False, move=False):
        """Print the sale line description on the done delivery slip.

        The done slip renders ``_get_aggregated_product_quantities``, whose
        ``description`` comes from the product alone. Swapping in the fuller
        text here makes it show up on every branch of the report (packages,
        kits, plain lines) without touching the templates.
        """
        properties = super()._get_aggregated_properties(move_line=move_line, move=move)
        source = move or (move_line and move_line.move_id)
        if not source:
            return properties
        description = source._unico_delivery_slip_description()
        properties["description"] = description
        # The description is part of the upstream grouping key, so the richer
        # text has to be reflected in it too — otherwise two sale lines of the
        # same product with different descriptions would merge into one row
        # and only one of the two descriptions would survive. Configured lines
        # additionally key on the sale line: each one is its own build.
        digest = hashlib.md5(
            description.encode(), usedforsecurity=False,
        ).hexdigest()[:12]
        if source._unico_is_unique_unit_line():
            digest = f"{digest}-{source.sudo().sale_line_id.id}"
        properties["line_key"] = f"{properties['line_key']}|u{digest}"
        return properties

    def _get_aggregated_product_quantities(self, **kwargs):
        aggregated_lines = super()._get_aggregated_product_quantities(**kwargs)
        return self._unico_split_unique_unit_lines(aggregated_lines)

    def _unico_split_unique_unit_lines(self, aggregated_lines):
        """Explode configured lines into one row per unit.

        Each configured door is individually made, so a line of three has to
        read as three products of one — not one product of three.
        """
        split_lines = {}
        for line_key, values in aggregated_lines.items():
            move = values.get("move")
            if not move or not move._unico_is_unique_unit_line():
                split_lines[line_key] = values
                continue
            qty_ordered = values.get("qty_ordered") or 0.0
            quantity = values.get("quantity") or 0.0
            count = move._unico_unit_split_count(qty_ordered, quantity)
            if count <= 1:
                split_lines[line_key] = values
                continue
            uom = values.get("product_uom")
            packaging_uom = values.get("packaging_uom_id")
            quantities = move._unico_split_unit_quantities(qty_ordered, quantity, count)
            for index, (row_ordered, row_quantity) in enumerate(quantities):
                row = dict(values)
                row["qty_ordered"] = row_ordered
                row["quantity"] = row_quantity
                if uom and packaging_uom and packaging_uom != uom:
                    row["packaging_qty_ordered"] = uom._compute_quantity(
                        row_ordered, packaging_uom
                    )
                    row["packaging_quantity"] = uom._compute_quantity(
                        row_quantity, packaging_uom
                    )
                split_lines[f"{line_key}#{index}"] = row
        return split_lines
