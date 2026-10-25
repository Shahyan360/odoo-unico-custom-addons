# Work in Progress (WIP) Board

One view of every **open customer order**, from the moment the customer confirms
to the moment the goods land with them — with the money, the supplier, the
promise date and the person chasing it all on the same row.

An order joins the board when it is **confirmed**, and leaves when it is **fully
delivered** or **cancelled**.

## The stages

An order sits on the rung of its **bottleneck** — the *earliest* stage that still
has open work, not the furthest it has reached. An order with one unanswered RFQ
reads **Purchasing** even if half of it already shipped, because that RFQ is what
is actually holding it up.

| Stage | What it means | What puts it here | What unblocks it |
|---|---|---|---|
| **Order Confirmed** | Customer has committed. Nothing bought, nothing shipped. | Confirmed order with no purchases raised and no outbound movement. | Raise the purchases, or ship from stock. |
| **Purchasing** | Still with the supplier. No agreed order yet. | At least one RFQ in *Draft* / *Sent* / *To Approve*. | Supplier confirms → the RFQ becomes a PO. |
| **Supplier Shipping** | Ordered, goods on the way. | A confirmed (or locked) PO whose goods have **not all arrived**. | Receive the goods → receipt status hits *Fully Received*. |
| **Goods Received** | Everything is in. Nothing has gone out yet. | All purchases fully received, no outbound transfer started. | Pick, pack and start the delivery. |
| **Out for Delivery** | On its way to the customer. | The outbound transfer is started or partly delivered. | Complete the delivery → the order leaves the board. |
| **On Hold** | Parked by hand, outside the normal flow. | **Nothing** — the system never puts an order here. Someone dragged it. | Unpin it (see below). |

> Stages are ordinary records, not code. Rename, reorder or add your own under
> **Sales → Configuration → WIP Stages**. A stage's *Automatic Rule* is what
> wires it to the detector; a stage with **no** rule is manual-only, like
> *On Hold*.

## Automatic, but you can overrule it

The stage is worked out for you and stays current on its own. If reality
disagrees — a shipment stuck in customs, a customer who asked you to pause —
**drag the card** to where it really is. That **pins** the stage, and the
detector stops moving it back.

A pinned order keeps showing what the system *would* have said, so a pin never
hides reality:

- The card is badged **Pinned**.
- The form shows **Detected Stage** next to the pinned one, with a warning banner.
- The filter **Pinned Off Detected Stage** and a dashboard tile list them all.

Press **Unpin Stage** to hand control back to the detector.

## What is on each row

| Group | Fields |
|---|---|
| Customer & product | Order, Customer, Products Ordered |
| Financials | Sales Value, Estimated Cost, Expected Margin, Margin %, Payment Status |
| Procurement | Supplier(s), PO Number(s) |
| Operations | Stage, Expected Delivery, Days Late, Urgency |
| Ownership | Responsible, Blocked, Delay / Blocker Note |

**Estimated Cost** is the cost of the quantities *ordered*, not delivered — the
delivered-basis figure reads zero for exactly the in-progress orders this board
exists to show.

**Urgency** comes from the expected delivery date (the commitment date when set,
otherwise the computed one): **Overdue** (past due), **At Risk** (due within
3 days), **On Track** (later).

## Where to look

| Surface | Use it for |
|---|---|
| **Dashboard** | Tiles by stage, urgency and payment, plus a *Needs Attention* watchlist of everything overdue or flagged. Every tile clicks through to exactly what it counted. |
| **Board** (kanban) | Day-to-day triage. Drag to pin, colour-coded by urgency. |
| **List** | Printing, exporting, and typing blocker notes inline. Use the ↗ button to open the sales order — the row itself is editable, so clicking it edits rather than navigates. |
| **Pivot / Graph** | Value and margin by stage. |

## Filtering the dashboard by date

Two dropdowns at the top of the dashboard:

**Period** — *All Time*, *This Week*, *Last Week*, *This Month*, *Last Month*, or
*Custom Range* (which reveals From / To date pickers). Weeks run **Monday to
Sunday**; months are calendar months. Dates are read in **your** timezone, so
"this week" means your week.

**Measured on** — which date the period applies to:

- **Expected Delivery** *(default)* — when it is due out. Answers *"what is
  landing this week?"*
- **Order Date** — when the customer confirmed. Answers *"what did we sell last
  month that is still open?"*

The filter applies to **everything below it** — the headline totals, every tile,
and the watchlist — and it travels inside each tile's click-through, so a tile
always opens exactly the orders it counted. The active window is spelled out
next to the order count so a filtered total is never mistaken for the whole
board.

> **Period defaults to All Time on purpose.** A WIP board that hides open work by
> default would bury the very thing it exists to surface — an order that went
> overdue three months ago. Narrow it deliberately, not by accident.

## Ownership and escalations

**Responsible** defaults to the order's salesperson but can be reassigned to
whoever actually owns the follow-up. **Blocked** puts the order on the watchlist
regardless of stage, and the **Delay / Blocker Note** records why. All three are
tracked in the chatter, so an escalation has a history.

## How it stays current

Most of it updates live. Two things cannot, and an **hourly cron** handles them:

1. **Purchases linked by origin.** Some purchases reference their sale only by
   name on the PO's *Source Document*, which the database cannot watch. (CPQ
   special-order purchases use a real link and *do* update live.)
2. **Anything relative to today** — urgency and days-late.

The same cron opens trackers for newly confirmed orders and archives finished
ones, so the board is self-healing: it backfills orders confirmed before the
module was installed, and nothing lingers after delivery.

## Access

Two groups, under **WIP Board Access Level**:

- **WIP User** — read the board, move cards, own and annotate blockers.
  Read-only on sales and purchase orders: they can see an order but not price,
  confirm or cancel it.
- **WIP Manager** — the above, plus configuring stages and removing trackers.

Salespeople get the same board access through their existing group.

## For developers

Two models, deliberately **not** more fields on `sale.order`:

- **`unico.wip.stage`** — the stages. `auto_token` binds a stage to a detector
  rule; a constraint keeps it one stage per rule.
- **`unico.wip.item`** — one tracker per open order. Carries the stage and the
  manual layer; everything factual is a `related` to the order, so there is no
  second copy of the truth to drift.

The ladder itself lives in `sale.order._unico_wip_stage_token()`, and
`_unico_wip_purchase_map()` resolves sale → purchase for a whole batch in one
query (matching exact origin *tokens*, so `S00123` never picks up `S001234`).

Tests: `tests/test_wip_report.py`.
