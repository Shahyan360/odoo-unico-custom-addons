"""Backfill CPQ special RFQ purchase-line ownership.

``unico_cpq_special_sale_line_id`` was added so RFQ refreshes can update or
remove only automation-managed component lines while preserving buyer-added
lines. Existing draft RFQ lines created before the marker can still be linked
through ``sale.order.line.cpq.special.component.purchase_line_id``; copy that
genuine relationship into the new marker on upgrade.

Only the real component -> purchase-line link is trusted. A component that has
lost its ``purchase_line_id`` is deliberately left unmarked: there is no
reliable way to tell a legacy automation line apart from a buyer-added line
that happens to share the same product and default name, and guessing would let
a later refresh overwrite or delete a manual purchase line. Such legacy RFQs are
instead flagged for review at runtime (a same-product line automation did not
create), which is the safe, recoverable outcome.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    component_model = env["sale.order.line.cpq.special.component"].sudo()
    components = component_model.search([
        ("sale_line_id", "!=", False),
        ("purchase_line_id", "!=", False),
    ])
    for component in components:
        purchase_line = component.purchase_line_id
        if (
            purchase_line.order_id.state == "draft"
            and not purchase_line.unico_cpq_special_sale_line_id
        ):
            purchase_line.unico_cpq_special_sale_line_id = component.sale_line_id.id
