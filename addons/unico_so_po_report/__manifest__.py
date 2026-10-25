# -*- coding: utf-8 -*-
{
    "name": "Unico Sales vs Purchases Report",
    "version": "19.0.1.0.0",
    "category": "Sales/Reporting",
    "summary": "Ask for a date range and an order status, get one Excel file of "
               "every sales order with its linked purchase orders, the vendors "
               "behind them and the revenue left on each order.",
    "description": """
Unico Sales vs Purchases Report
===============================
The report operations asks for by hand every month: **which open sales orders
have purchases hanging off them, who the vendors are, and what is left over.**

Run it from *Sales > Reporting > Sales vs Purchases*. The wizard asks three
things — a **date range**, a **sales order status**, and whether to count RFQs
that the supplier has not confirmed yet — shows the totals on screen, and
exports an ``.xlsx`` workbook with three sheets:

* **Sales vs Purchases** — one row per sales order: customer, salesperson,
  status, delivery and invoicing progress, the sale value, the number of linked
  purchases, the vendors, the purchase value, and **Revenue = sale untaxed −
  purchase untaxed**.
* **Purchase Detail** — one row per (sales order, purchase order) pair, so a
  disputed number can be traced to the PO it came from.
* **Vendor Summary** — spend and order count per vendor across the period.

Sale <-> purchase matching
--------------------------
Reuses ``sale.order._unico_wip_purchase_map()`` from unico_wip_report rather
than re-deriving the link, so this report and the WIP board can never disagree
about which purchases belong to a sale. That covers both routes: the stored
``cpq_special_purchase_ids`` relation, and purchases whose ``origin`` names the
sale (MTO / replenishment / manual RFQs).

Money
-----
Every figure is converted to the **company currency** at the order's own date,
so a report mixing currencies still adds up. Cancelled purchases are always
excluded; RFQs are included by default and can be switched off.
    """,
    "author": "Klouded Inc",

    "license": "LGPL-3",
    "depends": [
        # _unico_wip_purchase_map / _unico_wip_origin_tokens — the one place
        # sale <-> purchase matching is defined. Pulls unico_purchase,
        # unico_delivery, klouded_cpq, sale_stock and purchase_stock with it.
        "unico_wip_report",
        # sale.order.margin / margin_percent (the "Odoo Margin" column).
        "sale_margin",
        # purchase.order.receipt_status on the detail sheet.
        "purchase_stock",
    ],
    "data": [
        "security/ir.model.access.csv",
        "wizard/so_po_report_wizard_views.xml",
    ],
    "external_dependencies": {
        # Ships with Odoo core (the web client's own xlsx export uses it).
        "python": ["xlsxwriter"],
    },
    "application": False,
    "installable": True,
    "auto_install": False,
}
