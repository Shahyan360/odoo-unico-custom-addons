{
    "name": "Stripe Terminal Integration (Sales)",
    "version": "19.0.5.8.0",
    "category": "Sales",
    "summary": "Collect card (Stripe Terminal) or cash payments directly from a "
               "Sales Order using Odoo's Stripe payment provider "
               "(server-driven, WisePOS E / Stripe S700 + simulated reader).",
    "description": """
Sale Order Payments - Stripe Terminal & Cash
============================================

Collect payments straight from the Sales Order backend - by card on a physical
Stripe Terminal reader (WisePOS E, Stripe Reader S700) or in cash. No POS, no
invoice required.

Credentials reuse Odoo's built-in Stripe payment provider (payment_stripe):
Publishable Key, Secret Key, Webhook Signing Secret, Test/Enabled state and the
payment journal are all configured there.

Features
--------
* Reuses the Odoo Stripe Payment Provider for keys, mode (test/live) and journal.
* Sync Terminal locations and readers from your Stripe account.
* Create a simulated reader to test the full flow without hardware.
* "Collect Payment" button on the Sales Order opens a popup to take a Cash or
  Stripe Terminal (card_present) payment - shown while the order has an amount due.
* Reconciles against the order's open invoice when present, otherwise records a
  standalone payment on the order.
* Optional taxable Stripe customer surcharge via the stripe_charge configuration.
* Real-time status via Stripe webhooks, plus a manual "Check Status" button and a
  safety-net polling cron; refunds from the payment record.
""",
    "author": "Klouded Inc",

    "depends": [
        "base",
        "mail",
        "sale",
        "sale_management",
        "account",
        "payment",
        "payment_stripe",
        "stripe_charge",
        "product",
    ],
    "external_dependencies": {
        "python": ["stripe"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "data/ir_cron_data.xml",
        "views/stripe_terminal_location_views.xml",
        "views/stripe_terminal_reader_views.xml",
        "views/stripe_terminal_payment_views.xml",
        "views/payment_provider_views.xml",
        "views/payment_form_templates.xml",
        "views/payment_transaction_views.xml",
        "views/sale_order_views.xml",
        "wizard/stripe_terminal_payment_wizard_views.xml",
        "wizard/stripe_terminal_refund_wizard_views.xml",
        "wizard/payment_transaction_manual_confirm_wizard_views.xml",
        "views/menus.xml",
    ],
    "images": ["static/description/icon.png"],
    "application": True,
    "auto_install": False,
    "installable": True,
    "license": "LGPL-3",
}
