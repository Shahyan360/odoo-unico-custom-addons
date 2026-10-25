# Sales vs Purchases Report

One Excel file answering the question operations asks by hand every month:
**which sales orders have purchases hanging off them, who the vendors are, and
what is left over on each order.**

Run it from **Sales ▸ Reporting ▸ Sales vs Purchases**.

## The wizard

| Field | What it does |
|---|---|
| **From / To** | The sales order's own date (`Order Date`), read in your timezone, both ends inclusive. |
| **Order Status** | *Open orders* (confirmed, not fully delivered — the operations reading, and the default), *All confirmed*, *Quotations only*, *Everything except cancelled*, or *Everything*. |
| **Coverage** | *Only orders with purchase orders* (default) or *All orders in the period* — the second shows what was sourced from stock instead. |
| **Count RFQs** | On by default: purchases the supplier has not confirmed yet (RFQ / RFQ Sent / To Approve) still count. Switch off to report agreed purchase orders only. |
| **Company** | Multi-company only. Every amount is converted into this company's currency. |

Before you export, the wizard shows the order count, the purchase count and the
three money totals, so a wrong date range is caught on screen rather than in the
spreadsheet. **View Orders** opens the same selection as an ordinary Odoo list.

## The workbook

* **Sales vs Purchases** — one row per sales order: customer, salesperson, order
  date, status, delivery and invoicing progress, sale untaxed and total, number
  of linked purchases, **the PO numbers**, the vendors, purchase untaxed,
  **Revenue**, Revenue %, and Odoo's own margin figure for comparison. Sorted worst revenue first, with
  a totals row and a filter on every column.
* **Purchase Detail** — one row per (sales order, purchase order) pair: PO
  number, vendor, PO status, date, receipt status and the source document, so a
  disputed number traces back to the PO it came from.
* **Vendor Summary** — purchase orders, sales orders served, spend and share of
  total spend per vendor.

## How a purchase is matched to a sale

The report does **not** re-derive the link. It calls
`sale.order._unico_wip_purchase_map()` from `unico_wip_report`, the single place
that matching is defined, so this report and the WIP board can never disagree.
That covers both routes:

* the stored `cpq_special_purchase_ids` relation `klouded_cpq` stamps on
  special-order component purchases, and
* purchases whose **`origin`** names the sale (`S00123`, `S00123 / 4`, or a
  comma-separated run of both) — how the MTO, replenishment and manual RFQ flows
  link back. Matching is on exact origin *tokens*, so `S00123` never picks up
  `S001234`.

## What "Revenue" means here

**Revenue = sales untaxed − linked purchases untaxed**, in the company currency,
each side converted at its own order date.

Read it as *what the order has left after what was bought against it* — not as
landed margin. It excludes freight, stock consumed from inventory rather than
bought against the order, and labour. A purchase raised against several sales
orders is counted **in full against each** of them, so those rows read
pessimistically; the Purchase Detail sheet shows the shared PO on every sale it
touches, which is where to check when a number looks wrong. The **Odoo Margin**
column carries `sale.order.margin` (cost-based) alongside, for comparison.

Cancelled purchases never count.

## Access

Sales Managers and Purchase Users. The report exposes purchase cost against
sale value, so plain salespeople are deliberately not on the list.
