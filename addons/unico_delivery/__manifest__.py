{
    "name": "Unico Delivery",
    "version": "19.0.9.17.0",
    "category": "Inventory",
    "summary": "Fulfillment type on sales orders, lead-time driven commitment dates, "
               "and delivery/pickup & RMA cards on the inventory dashboard.",
    "description": """
Unico Delivery
==============
* Adds a *Fulfillment Type* (Delivery / Will Call - Customer Pickup) on sale orders.
* Adds a group-by filter for the fulfillment type.
* Splits the Delivery Orders dashboard card counts by fulfillment type.
* Leaves the Delivery Date empty on quotations and, on confirmation, fills a blank one
  from the maximum product Customer Lead Time of the order lines.
* Adds a dedicated RMA operations card to the inventory dashboard (filter based, no new operation type).
    """,
    "author": "Klouded Inc",

    "depends": [
        "sale_management",
        "sale_stock",
        "stock",
        "purchase",
        "sale_mrp",
        "klouded_cpq",
        "unico_rma",
        "unico_stripe_terminal",
    ],
    "data": [
        "reports/fulfillment_report_templates.xml",
        "reports/stock_delivery_report_templates.xml",
        "reports/spf_report.xml",
        "reports/purchase_order_report_templates.xml",
        "views/res_config_settings_views.xml",
        "views/sale_order_views.xml",
        "views/sale_order_line_views.xml",
        "views/purchase_order_views.xml",
        "views/stock_picking_type_views.xml",
        "views/stock_picking_list_views.xml",
        "views/stock_picking_form_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_delivery/static/src/rma_dashboard/rma_dashboard.js",
            "unico_delivery/static/src/rma_dashboard/rma_dashboard.xml",
            "unico_delivery/static/src/sale_dashboard/sale_dashboard.scss",
            "unico_delivery/static/src/sale_dashboard/sale_dashboard.js",
            "unico_delivery/static/src/sale_dashboard/sale_list_view.js",
            "unico_delivery/static/src/sale_dashboard/sale_dashboard.xml",
        ],
    },
    "application": False,
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
