{
    "name": "Unico Purchase",
    "version": "19.0.9.7.2",
    "category": "Purchases",
    "summary": "Trigger reordering on sale confirmation, surface the purchases needed "
               "to complete a sale, and rank RFQs by sales-driven urgency.",
    "description": """
Unico Purchase
==============
Ties purchasing to the sales pipeline:

* **Reorder on sale confirmation** — when a Sales Order is confirmed, the
  *auto* Min/Max reordering rules for its products are procured immediately
  (manual rules still just surface on the Replenishment dashboard).
* **Purchases needed to complete an order** — a smart button on the Sales Order
  shows the related open purchases and the number of units still short.
* **Sales-driven urgency on the Purchase dashboard** — every open RFQ/PO is
  ranked Overdue / Urgent / Normal from the earliest linked sale's commitment
  date, with urgent rows highlighted and clickable urgency tiles added to the
  existing Purchase dashboard so the buyer can track and act in one place.
    """,
    "author": "Klouded Inc",

    "depends": [
        # Core only — this module references no Unico-specific symbol, just the
        # standard sale ``commitment_date`` (sale_stock), purchase and stock.
        # Keeping deps to core makes it installable independently of the rest of
        # the Unico stack.
        "purchase",
        "purchase_stock",
        "sale_stock",
        "stock",
    ],
    "data": [
        "data/ir_cron.xml",
        "views/sale_order_views.xml",
        "views/sale_portal_templates.xml",
        "views/purchase_order_views.xml",
        "views/stock_orderpoint_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_purchase/static/src/purchase_dashboard/purchase_dashboard.js",
            "unico_purchase/static/src/purchase_dashboard/purchase_dashboard.xml",
            "unico_purchase/static/src/purchase_dashboard/purchase_dashboard.scss",
        ],
    },
    "application": False,
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
