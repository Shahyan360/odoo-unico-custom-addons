# Unico — Odoo 19 Custom Addons

A collection of Odoo 19 addons for connected sales, purchasing, warehouse operations, returns, payments and management reporting.

The modules extend standard Odoo models and workflows to help teams track customer orders from quotation through procurement, fulfillment and payment. This repository contains custom addon source; Odoo itself is installed separately.

## Workflow highlights

- **Sales and procurement:** sales-driven replenishment, purchase urgency and linked sales/purchase reporting.
- **Warehouse operations:** delivery planning, material availability and work-in-progress tracking.
- **Payment workflows:** Net 30 approvals, combined invoice payments, ACH status and Stripe Terminal integration.
- **Management reporting:** financial KPIs and commissionable gross profit reporting.
- **Returns and devices:** return authorization workflows and printer tray monitoring.

## Technology

Odoo 19 · Python ORM · PostgreSQL through Odoo · XML/QWeb views · JavaScript/OWL · SCSS

## Modules

| Module | Purpose |
| --- | --- |
| `unico_ach_status` | One-click list of sale orders whose ACH payment has not cleared Stripe yet |
| `unico_commission_gp_reporting` | Monthly commissionable gross profit reporting with cash-collected payout eligibility. |
| `unico_delivery` | Fulfillment type on sales orders, lead-time driven commitment dates, and delivery/pickup & RMA cards on the inventory dashboard. |
| `unico_delivery_fsm` | Create Field Service delivery execution tasks from local Sales Orders. |
| `unico_financial_dashboard` | Week-over-week financial KPI dashboard for revenue, AR, returns, and payment operations. |
| `unico_materials_report` | Dashboard and lists of confirmed sales orders waiting on materials before they can be fulfilled / enter production. |
| `unico_payment_multiple_invoices` | Generate one payment link for several customer invoices at once (same partner, open balance). The customer pays the combined amount on the portal and all invoices are reconciled automatically. |
| `unico_printer_iot` | Monitor printer paper-tray levels over SNMP (e.g. Kyocera TASKalfa) from Odoo, via direct polling or a Windows IoT-box push agent. |
| `unico_purchase` | Trigger reordering on sale confirmation, surface the purchases needed to complete a sale, and rank RFQs by sales-driven urgency. |
| `unico_rma` | Return authorizations, product replacements, refunds and legacy return workflows. |
| `unico_sale_net30_approval` | Net 30 quote-approval bypass, automatic pay-online invoice emails, and the customer PO requirement. |
| `unico_so_po_report` | Ask for a date range and an order status, get one Excel file of every sales order with its linked purchase orders, the vendors behind them and the revenue left on each order. |
| `unico_stripe_terminal` | Collect card (Stripe Terminal) or cash payments directly from a Sales Order using Odoo's Stripe payment provider (server-driven, WisePOS E / Stripe S700 + simulated reader). |
| `unico_wip_report` | Work-in-progress board for open customer orders — configurable lifecycle stages, automatic stage detection with manual override, and escalation tracking on a model of its own. |

## Installation

1. Install Odoo 19 separately, with licensed Enterprise dependencies where required.
2. Add this repository's `addons` directory to your local `addons_path`.
3. Copy `odoo.conf.example` to an ignored local `odoo.conf` and set your own configuration.
4. Create a fresh development database, update the Apps list and install the desired module with its dependencies.

External custom dependencies not included: `klouded_cpq`, `stripe_charge`. These must be obtained separately. The CPQ dependency is excluded because its manifest declares OPL-1. Third-party payment connectors, themes and queue addons are also excluded. Some modules therefore cannot install from this export alone.

## Validation and scope

Preparation checks: **180 Python files** and **87 XML files** parsed successfully, with no missing manifest data/demo references.

Python syntax, XML syntax, manifest file references and Git exclusions were checked during preparation. Odoo runtime tests were not run for this export. Existing test fixtures use synthetic example.com addresses; no database records, filestore, exports, credentials, company configuration or CI/CD files are included. Images were omitted from the source-only export.

## Attribution and licensing

Published as a development portfolio by [Shahyan](https://github.com/Shahyan360). Original module author and LGPL-3 declarations are preserved. Module identifiers remain unchanged to keep Odoo references consistent. Inclusion here does not claim sole authorship or grant access to excluded proprietary dependencies.

See [NOTICE.md](NOTICE.md) for the repository scope and third-party dependency notes.
