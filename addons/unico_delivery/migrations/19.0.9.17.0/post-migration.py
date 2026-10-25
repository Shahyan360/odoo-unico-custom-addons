"""Clear the auto-set Delivery Date from quotations.

The Delivery Date used to be derived from the order's customer lead time while
the quotation was being edited; it is now left blank on the quotation and filled
on confirmation instead. Quotations already carrying a machine-generated date
would otherwise keep it forever (a filled Delivery Date is never recomputed), so
blank those out once on upgrade.

Only *auto-set* dates are cleared: the value has to still match the last
``unico_auto_commitment_date`` this module wrote. A date a Sales Manager typed
in, or edited after the automation set it, no longer matches and is left alone.
Confirmed and cancelled orders are never touched.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    quotations = env["sale.order"].search([
        ("state", "in", ("draft", "sent")),
        ("commitment_date", "!=", False),
        ("unico_auto_commitment_date", "!=", False),
    ]).filtered(
        lambda order: order.commitment_date == order.unico_auto_commitment_date
    )
    if quotations:
        quotations.write({
            "commitment_date": False,
            "unico_auto_commitment_date": False,
        })
