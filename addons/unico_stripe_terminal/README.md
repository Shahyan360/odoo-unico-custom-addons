# Stripe Terminal Integration (Sales)

Collect payments directly from a **Sales Order** in the Odoo backend — by
**card** on a physical Stripe Terminal reader (WisePOS E, Stripe Reader S700) or
in **cash** — no POS, no leaving the order screen, no invoice required.

This is a server-driven integration (Stripe's backend REST API only, no
JavaScript SDK), modelled on the third-party
`cr_stripe_terminal_integration` app but rebuilt for **Odoo 19**. Credentials
reuse Odoo's built-in **Stripe payment provider** (`payment_stripe`), money is
recorded via `account.payment`, and security uses `res.groups.privilege`.

## Features

- Reuses the **Odoo Stripe Payment Provider** for the Publishable Key, Secret
  Key, Webhook Signing Secret, Test/Enabled mode and payment journal — no custom
  settings page. A *Stripe Terminal* tab is added to that provider.
- Sync Terminal **locations** and **readers** from your Stripe account.
- Create a **simulated reader** to test the full flow without hardware.
- **Collect Payment** button on the Sales Order opens a popup to choose the
  method — **Cash** or **Stripe Terminal**:
  - *Stripe Terminal* creates a `card_present` PaymentIntent and processes it on
    the chosen reader (tracked as a `stripe.terminal.payment` record).
  - *Cash* posts an immediate inbound **`account.payment`** directly — no
    terminal record. Its journal and payment method come from the **Cash
    Journal** / **Cash Payment Method** settings on the provider, so cash can
    share the card journal and still be told apart by its payment method.
    Unset, cash simply follows the provider's Payment Journal.
  - In both cases, if the order has an **open posted invoice** the payment is
    **reconciled against it**; otherwise it is recorded as a standalone customer
    payment on the order (**no invoice required**). Every collected payment is
    linked back to the order via `account.payment.sale_order_id`.
- **Amount Due** on the order = order total − amount collected (card + cash,
  read from the linked `account.payment`s). The button is hidden once the due
  reaches zero; the **Payments** smart button lists those payments.
- **Customer surcharge** (Stripe only): reuses the `stripe_charge` provider
  configuration (`_card_compute_fees`). When enabled, the surcharge is added as
  a taxable product line or linked surcharge invoice and collected together
  with the base amount in **one gross payment**. Cash payments never carry the
  surcharge.
- Status updates via **Stripe webhooks**, plus a manual **Check Status** button
  and a safety-net polling cron (every 2 min).
- On success the order is auto-confirmed and an **`account.payment`** is posted:
  if the order already has a posted, open customer **invoice**, the payment is
  **registered against it** (`account.payment.register`) and reconciled — Odoo
  marks the invoice **Paid** when fully covered. With no invoice yet, a
  standalone customer payment (prepayment) is posted instead.
- **Refund** a captured payment from the transaction record.

## Requirements

- Odoo 19 with `sale_management`, `account`, `payment_stripe` and `stripe_charge`
  (the `stripe_charge` / `sync_payment_charge` fee modules provide the fee config).
- Python: `stripe` (`pip install stripe`, see repo `requirements.txt`).
- A Stripe account with Terminal enabled and at least one internet-connected
  smart reader (or the simulated reader for testing).

## Setup

There are two sides: configure **Stripe** (dashboard), then **Odoo** (provider).

### A. Stripe Dashboard

Everything below is done in **Test mode** — toggle **"Test mode"** (top-right of
the Stripe Dashboard) ON. Test and live each have their own keys, locations,
readers and webhooks; nothing crosses over.

1. **Enable Terminal.** Go to **More → Terminal** (or open
   <https://dashboard.stripe.com/terminal>) and activate it if prompted. Fill in
   the brief business profile if asked — in test mode this is instant.
2. **Get your test API keys.** **Developers → API keys**
   (<https://dashboard.stripe.com/test/apikeys>):
   - **Publishable key** → `pk_test_...`
   - **Secret key** → `sk_test_...` (click *Reveal*).
   Copy both.
3. **Create a Terminal location** (optional — you can also click *Sync
   Locations* in Odoo, but you need at least one location to register a reader).
   **Terminal → Locations → + New** (or
   <https://dashboard.stripe.com/test/terminal/locations>), give it any name and
   address, save. A simulated `tml_...` location is also created automatically by
   Stripe in test mode.
4. **Create a webhook endpoint** (recommended so status updates arrive
   automatically). **Developers → Webhooks → + Add endpoint**
   (<https://dashboard.stripe.com/test/webhooks>):
   - **Endpoint URL:** `https://<your-odoo-domain>/unico_stripe_terminal/webhook`
   - **Events to send:** `payment_intent.succeeded`,
     `payment_intent.payment_failed`, `payment_intent.canceled`,
     `charge.refunded`.
   - Save, then **Reveal** the **Signing secret** → `whsec_...`. Copy it.
   - *No public URL yet (localhost/Docker)?* You can skip this — Odoo's
     **Check Status** button and the 2-minute polling cron will pick up the
     result without a webhook. (Or expose Odoo with `stripe listen
     --forward-to localhost:8069/unico_stripe_terminal/webhook` / an ngrok
     tunnel and use the secret it prints.)

### B. Odoo

1. Install the module (`pip install stripe` in the Odoo environment first; it is
   already listed in the repo `requirements.txt`).
2. **Sales → Configuration → Payment Providers → Stripe** (Odoo's standard
   Stripe provider — created by `payment_stripe`):
   - **Credentials** tab: paste the **Publishable Key** (`pk_test_...`), the
     **Secret Key** (`sk_test_...`) and, if you created one, the **Webhook
     Signing Secret** (`whsec_...`).
   - **State:** select **Test Mode**.
   - **Configuration** tab: set the **Payment Journal** (a bank or cash journal).
   - Save.
3. Open the **Stripe Terminal** tab on the provider and click **Test
   Connection** (should say *Connection successful*), then **Sync Locations** and
   **Sync Readers**. Optionally set a **Default Terminal Location**.
4. Still on the **Stripe Terminal** tab, under **Cash Collection**, choose the
   **Cash Journal** that receives counter cash — it may be the same journal as
   the card payments — and then a **Cash Payment Method**, which lists only that
   journal's inbound payment methods. Leave the method empty to use the
   journal's default. To label cash distinctly while sharing the card journal,
   add an inbound payment method line named *Cash* to that journal first
   (**Accounting > Configuration > Journals > Incoming Payments**).

## Test in Test Mode — no hardware required

Stripe provides a **simulated reader** that behaves like a real WisePOS E but
needs no device. The module drives it automatically (it calls Stripe's
`present_payment_method` test helper) while the provider is in Test Mode.

1. Finish **Setup** above with the provider in **Test Mode**.
2. On the provider's **Stripe Terminal** tab, click **Create Simulated Reader**
   (only visible in Test Mode). It registers Stripe's `simulated-wpe` reader and
   opens it — its status should be **Online**. (You can also see it under
   **Sales → Stripe Terminal → Readers**.)
3. Create a **Sales Order** for any customer/product. The **Collect Payment**
   button shows while the order has an amount due (no invoice required). Click it.
4. In the popup choose **Stripe Terminal**, pick the **Simulated WisePOS E**
   reader, confirm the amount (defaults to the order's amount due; any customer
   surcharge is shown), then **Collect Payment**. A payment record opens in
   `processing`.
   (Choosing **Cash** instead posts an immediate cash payment with no reader.)
5. The simulated reader is auto-presented a **test card that succeeds**, so the
   record moves to **Succeeded** (immediately via the in-flow status check, or
   shortly after via the webhook / **Check Status** button / cron). On success
   an `account.payment` is posted, the order is **confirmed**, and the **Card
   Payments** smart button shows `1`.
6. **Try a decline / refund (optional).** Refunds: open the succeeded *Terminal
   Payment* and click **Refund** → it becomes **Refunded**. To exercise a
   failure end-to-end you need a real/simulated card that declines; the
   automated test suite (below) covers the decline path with a mocked Stripe.

> Test-mode payments are **not** real money and appear under
> **Payments** in the Stripe Dashboard test view. Card-present test charges use
> Stripe's built-in test cards — you do not enter card numbers anywhere.

## Usage (production)

1. Register your physical reader to a Location in the Stripe Dashboard (or via
   the reader's own pairing flow), then **Sync Readers** in Odoo.
2. Open a Sales Order → **Collect Payment** (shown while there is an amount due).
3. Choose **Stripe Terminal** (pick the reader/location) or **Cash**, confirm the
   amount, then **Collect Payment**.
4. For card, the reader prompts the customer to tap/insert; the payment record
   moves `processing → succeeded` (via webhook, **Check Status**, or the cron).
   Cash is posted immediately.
5. On success the payment is posted and reconciled against the order's open
   invoice when present (otherwise recorded on the order); the order is
   confirmed, the **Amount Due** drops, and the **Payments** smart button shows
   the count.

## Tests

A mocked test suite lives in `tests/` (no network calls — `stripe` is patched
with a `MagicMock`). It is built on `AccountTestInvoicingCommon` so payments
post against a real chart of accounts. Run it with:

```bash
odoo -d <db> -i unico_stripe_terminal --test-enable --stop-after-init
```

Coverage: amount conversion (incl. zero-decimal currencies), location/reader
sync upsert idempotency, the SO amount-due / amount-collected computes and the
button gating, the collect wizard, the full card happy-path (intent → reader →
succeeded → posted `account.payment` → order confirmed), invoice reconciliation,
customer surcharges (charged on top in one gross payment), **cash collection** as a plain
`account.payment` (standalone and invoice-reconciled, no terminal record),
failure/cancel/refund paths, webhook status application, `_on_succeeded`
idempotency, and the polling cron.

## Notes

- The flow targets internet-connected smart readers; the simulated reader is
  driven automatically in test mode.
- Webhooks are the primary status source; the Check Status button and cron are
  safety nets so the flow works even before the webhook is configured.
- Replace `static/description/icon.png` with a branded icon if desired.
