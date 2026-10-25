"""Recompute the stored payment status (``unico_payment_status``) for every order.

The status gained a third value, ``returned_credited``, for orders whose own
sale was returned and fully credited back through an RMA credit note, and now
also marks an order Paid when its invoices are fully covered by an applied
credit note (no collected payment). Because the field is stored, existing
orders keep their old value until a dependency changes — so refresh them all
once on upgrade.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    orders = env["sale.order"].search([])
    if not orders:
        return
    orders.invalidate_recordset(["unico_payment_status"])
    orders._compute_unico_payment_status()
    orders.flush_recordset(["unico_payment_status"])
