"""Recompute the stored payment status (``unico_payment_status``) for every order.

The status is now scoped to confirmed orders only: quotations and cancelled
orders are set to no status (False) instead of Paid/Unpaid, so the Sales
dashboard tiles (which count ``state='sale'``) and the "Payment Status" group-by
agree. Because the field is stored, existing orders keep their old value until a
dependency changes — so refresh them all once on upgrade.
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
