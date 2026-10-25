{
    "name": "Unico Waiting on Materials Report",
    "version": "19.0.2.0.0",
    "category": "Sales/Reporting",
    "summary": "Dashboard and lists of confirmed sales orders waiting on "
               "materials before they can be fulfilled / enter production.",
    "description": """
Unico Waiting on Materials Report
=================================
A sale-order-centric report of every confirmed order that is **waiting on
materials**, classified by what it is waiting on (a line can wait on more than
one at once — e.g. a special door "Awaiting Purchase + Manufacturing"):

* **Awaiting Stock** — plain stockable lines short on finished-goods stock
  (reuses the unico_delivery stock-shortfall signal).
* **Awaiting Purchase** — special-order lines waiting on an incoming purchase
  (RFQ / PO not yet received).
* **Awaiting Manufacturing** — configured & special doors whose manufacturing
  order and its SFP / pick-components transfer are waiting on component
  availability (mrp.production.reservation_state not 'assigned').

Each order is also bucketed by urgency — Overdue / Urgent (<= 3 days) /
Scheduled — from its commitment date.

Consumed three ways: an Owl **dashboard** with clickable urgency and category
tiles, an order-grain **list** (printable / exportable), and a line-grain
breakdown **list**.
    """,
    "author": "Klouded Inc",

    "depends": [
        # unico_stock_shortfall (normal-stock signal) + pulls klouded_cpq
        # (is_configurable / cpq_special_purchase_ids), sale_mrp
        # (mrp.production.sale_line_id), sale_stock and stock.
        "unico_delivery",
        # purchase.order.receipt_status (the "incoming purchase received" signal
        # lives in purchase_stock, not base purchase).
        "purchase_stock",
    ],
    "data": [
        "data/ir_cron.xml",
        "views/sale_order_views.xml",
        "views/sale_order_line_views.xml",
        "views/materials_report_actions.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_materials_report/static/src/materials_dashboard/materials_dashboard.scss",
            "unico_materials_report/static/src/materials_dashboard/materials_dashboard.js",
            "unico_materials_report/static/src/materials_dashboard/materials_dashboard.xml",
        ],
    },
    "application": False,
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
