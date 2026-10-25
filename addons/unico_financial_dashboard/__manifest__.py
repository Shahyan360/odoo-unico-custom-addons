{
    "name": "Unico Financial KPI Dashboard",
    "version": "19.0.3.5.0",
    "category": "Accounting/Reporting",
    "summary": "Week-over-week financial KPI dashboard for revenue, AR, returns, and payment operations.",
    "author": "Klouded Inc",
    "license": "LGPL-3",
    "depends": [
        "account",
        "sale_management",
        "sale_margin",
        "sale_stock",
        "stock_account",
        "unico_delivery",
        "unico_rma",
        "unico_stripe_terminal",
    ],
    "data": [
        "views/financial_dashboard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_financial_dashboard/static/src/financial_dashboard/financial_dashboard.scss",
            "unico_financial_dashboard/static/src/financial_dashboard/financial_dashboard.js",
            "unico_financial_dashboard/static/src/financial_dashboard/financial_dashboard.xml",
        ],
    },
    "application": False,
    "installable": True,
}
