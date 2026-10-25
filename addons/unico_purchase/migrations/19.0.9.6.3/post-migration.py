"""Pause per-source grouping where legacy draft RFQs still need cleanup.

RFQs created before ``unico_po_group_key`` was introduced have no source key.
If grouping became active immediately, new procurement would create a parallel
keyed RFQ instead of reusing those drafts. Disable the setting only for affected
companies; administrators can review the legacy drafts and enable it afterward.
Companies without legacy drafts, and companies created later, keep the enabled
default.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    legacy_rfqs = env["purchase.order"].sudo().search([
        ("state", "in", ("draft", "sent")),
        ("unico_po_group_key", "=", False),
    ])
    companies = legacy_rfqs.company_id.filtered("unico_group_rfq_per_source")
    if companies:
        companies.write({"unico_group_rfq_per_source": False})
