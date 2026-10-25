{
    "name": "Unico Sale - Customer Billing Policies",
    "version": "19.0.2.2.0",
    "category": "Sales",
    "summary": "Net 30 quote-approval bypass, automatic pay-online invoice "
               "emails, and the customer PO requirement.",
    "description": """
Unico Sale - Customer Billing Policies
======================================
Rules that follow from *who the customer is and how they pay*: what a Net 30
payment term means at Unico, and which customers must supply their own purchase
order before an order can be worked.

**1. Net 30 quotes skip the margin/discount approval.**
Customers on the Net 30 Days payment term may confirm quotes / progress sales
orders without the manual approval. The gate is a Studio approval rule (model
``sale.order``) created through the Studio UI, so it lives only in the database
with no external id. This module relaxes it in code so the change is versioned
and deploys with the codebase: on every install/upgrade it *injects* a
``payment_term_id != Net 30`` exclusion into the rule's domain (existing
margin/discount thresholds are preserved). Idempotent: re-running never stacks
duplicate clauses.

**2. Net 30 invoices are emailed on validation, with a pay-online link.**
Validating a customer invoice whose payment term is a Net 30 term mails it to
the customer straight away, carrying the invoice PDF and a button through to
the portal, where the invoice can be read and paid. Odoo's own invoice template
has no portal link, so a dedicated template ships here. Any term whose name
matches ``net 30`` counts ("Net 30 Days", "NET 30", "net30"). Controlled by
*Auto-send Net 30 Invoices* in Accounting settings (on by default), separate
from the general Automatic Customer Emails switch.

**3. Flagged customers must supply their purchase order.**
Some customers will not pay an invoice that does not quote their own PO number,
so it is captured before the order is worked rather than discovered at billing
time. **PO Alert** (checkbox on the customer) marks such a customer, **PO
Detail** (on the sales order, under Payment Terms) records their PO, and
confirming an order for a flagged customer with PO Detail empty is refused. The
flag is read from the customer's *company* contact, so ticking it on a company
covers every contact underneath it.
    """,
    "author": "Klouded Inc",

    # sale provides the gated model, sale_margin provides the sale.order margin
    # field used by the approval notification, web_studio provides
    # studio.approval.rule, mail provides mail.template, and account provides
    # the invoice + payment term the auto-send is built on.
    "depends": ["sale", "sale_margin", "web_studio", "mail", "account"],
    "data": [
        "data/quote_internal_approval_email.xml",
        "data/net30_invoice_email.xml",
        "data/net30_approval_bypass.xml",
        "views/res_config_settings_views.xml",
        "views/res_partner_views.xml",
        "views/sale_order_views.xml",
    ],
    "application": False,
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
