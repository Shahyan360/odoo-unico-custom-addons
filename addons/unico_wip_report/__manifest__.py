# -*- coding: utf-8 -*-
{
    "name": "Unico Work-in-Progress (WIP) Board",
    "version": "19.0.2.0.0",
    "category": "Sales/Reporting",
    "summary": "Work-in-progress board for open customer orders — configurable "
               "lifecycle stages, automatic stage detection with manual "
               "override, and escalation tracking on a model of its own.",
    "description": """
Unico Work-in-Progress (WIP) Board
==================================
Complete visibility over every **open customer order**, from the moment the
customer confirms to the moment the goods land with them.

Two models
----------
* ``unico.wip.stage`` — the lifecycle stages, as ordinary configurable records.
  Rename, reorder or add your own from *Sales > Configuration > WIP Stages*. A
  stage's **Automatic Rule** is what wires it to the detector; a stage with no
  rule is manual-only (drag an order onto it, the detector never will).
* ``unico.wip.item`` — one tracker per open order, carrying the resolved stage
  and the operations layer (owner, blocked flag, blocker note, chatter).

Deliberately *not* more fields on ``sale.order``: the manual layer here is
operations data, not sales data, so an ops coordinator can run the whole board
without write access to sale orders. Everything factual is a ``related`` to the
order, so there is no second copy of the truth to drift.

Stage detection
---------------
An order is placed on the rung of its **bottleneck** — the earliest stage that
still has open work — so an order with an unanswered RFQ reads *Purchasing* even
if part of it has already shipped. The default ladder:

* **Order Confirmed** — confirmed, nothing purchased or shipped yet.
* **Purchasing** — an RFQ is still sitting with the supplier.
* **Supplier Shipping** — ordered, goods not all arrived.
* **Goods Received** — everything in, nothing out to the customer yet.
* **Out for Delivery** — on its way to the customer.
* **On Hold** — manual-only parking stage, outside the normal flow.

Dragging a card **pins** the stage (``stage_override``), which stops the
detector moving it back; the detected stage is still shown alongside, and a
card pinned away from it is badged so the disagreement stays visible. *Unpin
Stage* hands control back.

Surfaces: an Owl **dashboard** of clickable tiles with an overdue-and-blocked
watchlist, the **kanban** board, an inline-editable **list** (printable,
exportable), and **pivot** / **graph** for value-by-stage analysis.

Notes
-----
Purchases link to their sale two ways — the stored ``cpq_special_purchase_ids``
relation (live) and the purchase ``origin`` naming the sale (a Char, so an
hourly cron refreshes it, along with tracker creation/archival and the
time-relative urgency).
    """,
    "author": "Klouded Inc",

    "license": "LGPL-3",
    "depends": [
        # sale.order.margin / margin_percent and sale.order.line.purchase_price.
        "sale_margin",
        # sale.order.delivery_status (the outbound-progress signal).
        "sale_stock",
        # purchase.order.receipt_status (the "goods arrived" signal).
        "purchase_stock",
        # unico_payment_status + fulfillment_type; also pulls klouded_cpq for
        # cpq_special_purchase_ids / is_configurable.
        "unico_delivery",
        # origin-based sale <-> purchase matching conventions.
        "unico_purchase",
        # chatter + activities on the tracker (escalation history).
        "mail",
    ],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/wip_stage_data.xml",
        "data/ir_cron.xml",
        "views/wip_stage_views.xml",
        "views/wip_item_views.xml",
        "views/sale_order_views.xml",
        "views/wip_report_actions.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "unico_wip_report/static/src/wip_dashboard/wip_dashboard.scss",
            "unico_wip_report/static/src/wip_dashboard/wip_dashboard.js",
            "unico_wip_report/static/src/wip_dashboard/wip_dashboard.xml",
        ],
    },
    "application": False,
    "installable": True,
    "auto_install": False,
}
