"""Recompute the stored Paid ribbon (``unico_payment_status``) for every order.

The payment status now also recognises manual ``account.payment`` records and
online/portal payments (which reconcile against the invoice rather than carrying
a ``sale_order_id``). Because the field is stored, existing orders keep their old
value until a dependency changes — so refresh them all once on upgrade.
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
