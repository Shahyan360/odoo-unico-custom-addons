# Part of Odoo. See LICENSE file for full copyright and licensing details.
{
    "name": "Payment - Multiple Invoices",
    "version": "19.0.1.7.3",
    "category": "Accounting/Accounting",
    "summary": "Generate one payment link for several customer invoices at once "
    "(same partner, open balance). The customer pays the combined amount on the "
    "portal and all invoices are reconciled automatically.",
    "description": """
Payment Multiple Invoices
=========================

Generate **one payment link** for **several customer invoices** at once
(same partner, open balance).

Features
--------
* Select several posted, unpaid/partial customer invoices in the list view.
* Run *Generate Payment Link* from the *Action* menu.
* A wizard shows a single secure payment URL covering the combined residual.
* Share the link; the customer pays the total on the standard Odoo portal.
* Once the transaction is confirmed, **all** invoices are reconciled
  automatically against the created payment.

Set Line Total
--------------

Sales negotiates a figure for the whole order line, not a unit price. A button
beside the line total opens a popup that takes the agreed total and derives the
Unit Price from it, previewing the resulting margin before anything is written.
``price_subtotal`` stays computed, so margin keeps following Odoo's own
computation.
    """,
    "author": "Klouded Inc",

    "license": "LGPL-3",
    "depends": [
        "account_payment",
        # Set Line Total previews and writes margin, which sale_margin owns.
        "sale_margin",
        "stripe_charge",
    ],
    "data": [
        "security/ir.model.access.csv",
        "wizard/multi_invoice_payment_link_views.xml",
        "wizard/line_total_wizard_views.xml",
        "views/account_payment_views.xml",
        "views/sale_order_views.xml",
        "views/payment_templates.xml",
        "data/server_action.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_payment_multiple_invoices/static/src/line_total_button/line_total_button.js",
            "unico_payment_multiple_invoices/static/src/line_total_button/line_total_button.xml",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
