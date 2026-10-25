import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import stripe_client

_logger = logging.getLogger(__name__)

_ACTIVE_REFUND_STATUSES = (
    "draft",
    "pending",
    "requires_action",
    "manual_review",
)


class StripeTerminalPayment(models.Model):
    _name = "stripe.terminal.payment"
    _description = "Stripe Terminal Payment"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    _payment_intent_unique = models.Constraint(
        "unique(payment_intent_id)",
        "A Stripe PaymentIntent can only be linked to one terminal payment.",
    )

    name = fields.Char(string="Reference", required=True, copy=False, readonly=True,
                       default=lambda self: _("New"))
    sale_order_id = fields.Many2one(
        comodel_name="sale.order",
        string="Sales Order",
        required=True,
        ondelete="cascade",
        tracking=True,
    )
    partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Customer",
        required=True,
    )
    amount = fields.Monetary(string="Amount", required=True)
    currency_id = fields.Many2one(
        comodel_name="res.currency",
        string="Currency",
        required=True,
    )
    reader_id = fields.Many2one(
        comodel_name="stripe.terminal.reader",
        string="Reader",
        required=True,
    )
    fee_amount = fields.Monetary(
        string="Card Surcharge",
        copy=False,
        help="Customer surcharge, including applicable tax, charged on top of the amount.",
    )
    surcharge_net_amount = fields.Monetary(
        string="Surcharge Before Tax",
        copy=False,
    )
    stripe_card_funding = fields.Selection(
        selection=[
            ("unknown", "Unknown"),
            ("credit", "Credit"),
            ("debit", "Debit"),
            ("prepaid", "Prepaid"),
        ],
        string="Card Funding",
        default="unknown",
        readonly=True,
        copy=False,
        tracking=True,
        help="Funding classification returned by Stripe after card collection.",
    )
    collection_phase = fields.Selection(
        selection=[
            ("direct", "Direct Processing"),
            ("collecting", "Collecting Card"),
            ("confirming", "Confirming Payment"),
            ("complete", "Complete"),
        ],
        string="Reader Phase",
        default="direct",
        readonly=True,
        copy=False,
    )
    total_charged = fields.Monetary(
        string="Total Charged",
        compute="_compute_total_charged",
        help="Amount actually charged on the card (amount + card fee).",
    )
    payment_intent_id = fields.Char(string="Stripe PaymentIntent", index=True, copy=False)
    charge_id = fields.Char(string="Stripe Charge", copy=False)
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("processing", "Processing"),
            ("requires_action", "Requires Action"),
            ("succeeded", "Succeeded"),
            ("failed", "Failed"),
            ("canceled", "Canceled"),
            ("refunded", "Refunded"),
        ],
        string="Status",
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    refunded_amount = fields.Monetary(string="Refunded Amount", copy=False)
    refunded_surcharge_net_amount = fields.Monetary(
        string="Refunded Surcharge Before Tax",
        copy=False,
    )
    refunded_surcharge_amount = fields.Monetary(
        string="Refunded Surcharge",
        copy=False,
    )
    surcharge_refund_move_ids = fields.Many2many(
        comodel_name="account.move",
        relation="stripe_terminal_payment_refund_move_rel",
        column1="terminal_payment_id",
        column2="move_id",
        string="Surcharge Credit Notes",
        copy=False,
    )
    surcharge_refund_sale_line_ids = fields.Many2many(
        comodel_name="sale.order.line",
        relation="stripe_terminal_payment_refund_sale_line_rel",
        column1="terminal_payment_id",
        column2="sale_line_id",
        string="Surcharge Refund Order Lines",
        copy=False,
    )
    refund_ids = fields.One2many(
        comodel_name="stripe.terminal.refund",
        inverse_name="payment_id",
        string="Stripe Refunds",
        readonly=True,
        copy=False,
    )
    error_message = fields.Char(string="Last Error", copy=False)
    account_payment_id = fields.Many2one(
        comodel_name="account.payment",
        string="Odoo Payment",
        copy=False,
    )
    fee_account_payment_id = fields.Many2one(
        comodel_name="account.payment",
        string="Legacy Fee Payment",
        copy=False,
        help="Historical separate fee payment. New collections do not create one.",
    )
    fee_move_id = fields.Many2one(
        comodel_name="account.move",
        string="Surcharge Invoice",
        copy=False,
        ondelete="restrict",
    )
    surcharge_sale_order_line_id = fields.Many2one(
        comodel_name="sale.order.line",
        string="Surcharge Order Line",
        copy=False,
        ondelete="set null",
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )

    @api.model_create_multi
    def create(self, vals_list):
        seen_intents = set()
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "stripe.terminal.payment") or _("New")
            intent_id = self._normalize_payment_intent_id(vals.get("payment_intent_id"))
            if "payment_intent_id" in vals:
                vals["payment_intent_id"] = intent_id
            if intent_id:
                if intent_id in seen_intents:
                    self._raise_duplicate_payment_intent(intent_id)
                seen_intents.add(intent_id)
                self._check_payment_intent_id_available(intent_id)
        return super().create(vals_list)

    def write(self, vals):
        if "payment_intent_id" in vals:
            intent_id = self._normalize_payment_intent_id(vals.get("payment_intent_id"))
            vals = dict(vals, payment_intent_id=intent_id)
            if intent_id:
                if len(self) > 1:
                    self._raise_duplicate_payment_intent(intent_id)
                for payment in self:
                    payment._check_payment_intent_id_available(intent_id)
        return super().write(vals)

    def unlink(self):
        raise UserError(_(
            "Terminal payments cannot be deleted — they are kept as a financial "
            "audit record. Use Cancel or Refund to reverse a payment."))

    @api.model
    def _normalize_payment_intent_id(self, intent_id):
        return (intent_id or "").strip()

    def _check_payment_intent_id_available(self, intent_id):
        self.ensure_one() if self else None
        domain = [("payment_intent_id", "=", intent_id)]
        if self:
            domain.append(("id", "!=", self.id))
        duplicate = self.search(domain, limit=1)
        if duplicate:
            self._raise_duplicate_payment_intent(intent_id, duplicate)

    def _raise_duplicate_payment_intent(self, intent_id, duplicate=False):
        message = _("Stripe PaymentIntent %(intent)s is already linked to another terminal payment.")
        if duplicate:
            message = _("Stripe PaymentIntent %(intent)s is already linked to %(payment)s.")
        raise ValidationError(message % {
            "intent": intent_id,
            "payment": duplicate.display_name if duplicate else _("another terminal payment"),
        })

    @api.constrains("payment_intent_id")
    def _check_payment_intent_id_unique(self):
        for payment in self.filtered("payment_intent_id"):
            payment._check_payment_intent_id_available(payment.payment_intent_id)

    @api.depends("amount", "fee_amount")
    def _compute_total_charged(self):
        for payment in self:
            payment.total_charged = (payment.amount or 0.0) + (payment.fee_amount or 0.0)

    def _compute_terminal_fee(self, provider):
        """Card fee charged to the customer, per the provider's configuration.

        Reuses ``stripe_charge``'s provider helper so domestic/international and
        the "free above" rules stay consistent with the rest of the system.
        """
        self.ensure_one()
        if not getattr(provider, "fees_active", False):
            return 0.0
        country = self.partner_id.country_id or self.company_id.country_id
        fee = provider._card_compute_fees(self.amount, self.currency_id, country)
        return self.currency_id.round(fee or 0.0)

    def _prepare_terminal_surcharge(self, provider):
        """Create or reuse the taxable surcharge document before charging."""
        self.ensure_one()
        if self.stripe_card_funding != "credit":
            self.write({
                "surcharge_net_amount": 0.0,
                "fee_amount": 0.0,
            })
            return
        net_amount = self._compute_terminal_fee(provider)
        self.surcharge_net_amount = net_amount
        if not net_amount:
            self.fee_amount = 0.0
            return
        if self.fee_move_id and self.fee_move_id.state != "cancel":
            self.fee_amount = self.fee_move_id.amount_total
            return
        if self.surcharge_sale_order_line_id:
            self.fee_amount = self.surcharge_sale_order_line_id.price_total
            return

        open_invoices = self.sale_order_id._stripe_terminal_open_invoices()
        if open_invoices:
            source_invoice = provider._stripe_validate_surcharge_invoice_context(
                open_invoices
            )
            invoice = provider._stripe_create_surcharge_invoice(
                partner=self.partner_id,
                currency=self.currency_id,
                amount=net_amount,
                origin=self.sale_order_id.name,
                reference=_("Stripe Terminal surcharge for %s", self.name),
                source_invoice=source_invoice,
                source_order=self.sale_order_id,
            )
            self.write({
                "fee_move_id": invoice.id,
                "fee_amount": invoice.amount_total,
            })
            return

        product = provider._stripe_get_surcharge_product()
        line = self.env["sale.order.line"].sudo().create({
            "order_id": self.sale_order_id.id,
            "product_id": product.id,
            "product_uom_qty": 1.0,
            "product_uom_id": product.uom_id.id,
            "price_unit": net_amount,
            "is_stripe_surcharge": True,
            "stripe_terminal_payment_id": self.id,
        })
        self.write({
            "surcharge_sale_order_line_id": line.id,
            "fee_amount": line.price_total,
        })

    def _cleanup_unposted_terminal_surcharge(self):
        self.ensure_one()
        if self.fee_move_id and self.fee_move_id.state == "draft":
            invoice = self.fee_move_id
            self.fee_move_id = False
            invoice.unlink()
        if (
            self.surcharge_sale_order_line_id
            and not self.surcharge_sale_order_line_id.qty_invoiced
        ):
            line = self.surcharge_sale_order_line_id
            self.surcharge_sale_order_line_id = False
            line.unlink()

    # ------------------------------------------------------------------
    # Stripe processing
    # ------------------------------------------------------------------
    def action_process_on_reader(self):
        """Public entry point (buttons) to (re)send the payment to the reader."""
        self.ensure_one()
        return self._process_on_reader()

    def _process_on_reader(self):
        """Create a card_present PaymentIntent and push it to the reader."""
        self.ensure_one()
        if self.status not in ("draft", "failed", "canceled"):
            raise UserError(_("This payment is already %s.", self.status))
        if self.reader_id.status != "online":
            raise stripe_client.terminal_user_error(
                self.env,
                code="terminal_reader_offline",
                details=_(
                    "Reader '%s' is marked offline in Odoo. Sync its status and try again.",
                    self.reader_id.label),
            )
        stripe, provider = stripe_client.get_stripe_for_company(
            self.env, self.company_id)
        needs_funding_check = bool(getattr(provider, "fees_active", False))
        charge = self.amount
        amount = stripe_client.to_stripe_amount(charge, self.currency_id)
        try:
            if self.payment_intent_id:
                intent = stripe.PaymentIntent.retrieve(self.payment_intent_id)
            else:
                intent = stripe.PaymentIntent.create(
                    amount=amount,
                    currency=self.currency_id.name.lower(),
                    payment_method_types=["card_present"],
                    capture_method="automatic",
                    description=self.sale_order_id.name,
                    metadata={
                        "odoo_terminal_payment_id": str(self.id),
                        "odoo_sale_order_id": str(self.sale_order_id.id),
                        "odoo_sale_order": self.sale_order_id.name,
                        "odoo_card_fee": "pending" if needs_funding_check else "0",
                    },
                )
                self.payment_intent_id = stripe_client.sget(intent, "id")
            if needs_funding_check:
                stripe.terminal.Reader.collect_payment_method(
                    self.reader_id.stripe_reader_id,
                    payment_intent=self.payment_intent_id,
                )
                phase = "collecting"
            else:
                stripe.terminal.Reader.process_payment_intent(
                    self.reader_id.stripe_reader_id,
                    payment_intent=self.payment_intent_id,
                )
                phase = "direct"
        except Exception as ex:
            _logger.exception("Starting the Terminal reader payment failed")
            self.write({"status": "failed", "error_message": str(ex)})
            raise stripe_client.terminal_user_error(self.env, ex) from ex

        self.write({
            "status": "processing",
            "collection_phase": phase,
            "error_message": False,
        })

        # In test mode the simulated reader does not collect a real card, so we
        # present a test card on its behalf to drive the flow to completion.
        if self.reader_id.is_simulated and provider.state == "test":
            try:
                stripe.terminal.Reader.TestHelpers.present_payment_method(
                    self.reader_id.stripe_reader_id)
            except Exception as ex:  # pragma: no cover - best effort test helper
                _logger.info("Simulated present_payment_method skipped: %s", ex)

        self.action_check_status()
        return True

    def action_check_status(self):
        """Retrieve the PaymentIntent and reconcile the local status."""
        for payment in self:
            if not payment.payment_intent_id:
                continue
            stripe, dummy = stripe_client.get_stripe_for_company(
                self.env, payment.company_id)
            try:
                intent = stripe.PaymentIntent.retrieve(
                    payment.payment_intent_id,
                    expand=["payment_method"],
                )
            except Exception as ex:
                if payment._is_missing_payment_intent_error(ex):
                    message = str(ex)
                    payment.write({
                        "status": "failed",
                        "error_message": message,
                    })
                    _logger.info(
                        "PaymentIntent %s no longer exists; marked %s failed.",
                        payment.payment_intent_id, payment.name)
                    continue
                _logger.info(
                    "PaymentIntent retrieve failed for %s: %s",
                    payment.payment_intent_id, ex)
                raise UserError(_("Could not retrieve the payment status: %s", ex))
            payment._apply_intent_status(intent)
        return True

    def _is_missing_payment_intent_error(self, error):
        """Return whether Stripe says the saved PaymentIntent no longer exists."""
        self.ensure_one()
        code = getattr(error, "code", None)
        param = getattr(error, "param", None)
        return (
            code == "resource_missing"
            and param in (None, "intent", "payment_intent")
        ) or "No such payment_intent" in str(error)

    def _apply_intent_status(self, intent):
        """Map a Stripe PaymentIntent status onto this record."""
        self.ensure_one()
        sget = stripe_client.sget
        status = sget(intent, "status")
        # Newer Stripe API versions expose `latest_charge`; older ones expanded
        # `charges.data`. Support both.
        charge_id = sget(intent, "latest_charge")
        if not charge_id:
            charges = sget(sget(intent, "charges") or {}, "data") or []
            if charges:
                charge_id = sget(charges[0], "id")
        if charge_id and isinstance(charge_id, str):
            self.charge_id = charge_id
        last_error = sget(intent, "last_payment_error") or {}

        if (
            self.collection_phase == "collecting"
            and status == "requires_confirmation"
        ):
            self._confirm_collected_terminal_card(intent)
            return

        if status == "succeeded":
            if self.status != "succeeded":
                self.write({
                    "status": "succeeded",
                    "collection_phase": "complete",
                    "error_message": False,
                })
                self._on_succeeded()
        elif status in ("requires_payment_method",) and last_error:
            self.write({
                "status": "failed",
                "error_message": sget(last_error, "message") or _("Payment failed."),
            })
        elif status == "canceled":
            self.write({"status": "canceled"})
            self._cleanup_unposted_terminal_surcharge()
        elif status in ("requires_action", "requires_confirmation"):
            self.write({"status": "requires_action"})
        else:
            # requires_capture / processing / requires_payment_method (no error)
            if self.status not in ("succeeded", "refunded"):
                self.write({"status": "processing"})

    def _confirm_collected_terminal_card(self, intent):
        """Classify a collected card, calculate the final amount, and confirm it."""
        self.ensure_one()
        stripe, provider = stripe_client.get_stripe_for_company(
            self.env,
            self.company_id,
        )
        payment_method = stripe_client.sget(intent, "payment_method") or {}
        if isinstance(payment_method, str):
            payment_method = stripe.PaymentMethod.retrieve(payment_method)
        funding = provider._stripe_extract_card_funding(payment_method)
        self.stripe_card_funding = funding
        self._prepare_terminal_surcharge(provider)
        final_amount = stripe_client.to_stripe_amount(
            self.total_charged,
            self.currency_id,
        )
        try:
            stripe.PaymentIntent.modify(
                self.payment_intent_id,
                amount=final_amount,
                metadata={
                    "odoo_terminal_payment_id": str(self.id),
                    "odoo_sale_order_id": str(self.sale_order_id.id),
                    "odoo_sale_order": self.sale_order_id.name,
                    "odoo_card_funding": funding,
                    "odoo_card_fee": str(self.fee_amount),
                },
            )
            stripe.terminal.Reader.confirm_payment_intent(
                self.reader_id.stripe_reader_id,
                payment_intent=self.payment_intent_id,
            )
        except Exception as ex:
            _logger.exception("Confirming the Terminal payment failed")
            self.write({
                "status": "failed",
                "error_message": str(ex),
            })
            self._cleanup_unposted_terminal_surcharge()
            raise stripe_client.terminal_user_error(
                self.env, ex, stage="confirm") from ex
        self.write({
            "status": "processing",
            "collection_phase": "confirming",
            "error_message": False,
        })

        if self.reader_id.is_simulated and provider.state == "test":
            self.action_check_status()

    def _on_succeeded(self):
        """Confirm the order and register the Odoo payment on success."""
        self.ensure_one()
        self._lock_for_terminal_settlement()
        order = self.sale_order_id
        if order:
            order.invalidate_recordset(["state"])
        if order and order.state in ("draft", "sent"):
            try:
                order.action_confirm()
            except Exception as ex:  # pragma: no cover
                _logger.warning(
                    "Auto-confirm of %s after payment failed: %s", order.name, ex)
        if self.account_payment_id:
            return
        if not self.account_payment_id:
            self._register_account_payment()
        self.message_post(body=_(
            "Card payment of %(amount)s %(currency)s succeeded.",
            amount=self.total_charged, currency=self.currency_id.name))

    def _lock_for_terminal_settlement(self):
        """Serialize payment settlement for this terminal transaction.

        Webhooks and the polling cron can receive the same succeeded intent at
        the same time. Lock and reload before creating account.payment records so
        a second worker sees the payment created by the first worker.
        """
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM "%s" WHERE id = %%s FOR UPDATE' % self._table,
            [self.id],
        )
        self.invalidate_recordset([
            "status",
            "sale_order_id",
            "account_payment_id",
            "fee_account_payment_id",
            "fee_move_id",
            "surcharge_sale_order_line_id",
        ])

    def _get_journal(self):
        return self.env["payment.provider"]._get_terminal_settlement_journal(
            self.company_id)

    def _register_account_payment(self):
        """Post (and reconcile against any open invoice) the card payment via the
        shared sale.order helper, then keep a link to the created payment."""
        self.ensure_one()
        payments = self.sale_order_id._stripe_register_customer_payment(
            self.total_charged,
            self._get_journal(),
            "%s - %s" % (self.sale_order_id.name, self.name),
            surcharge_invoice=self.fee_move_id,
        )
        canonical_payment = payments[:1]
        if self.payment_intent_id and canonical_payment:
            canonical_payment.write({
                "stripe_external_payment_intent_id": self.payment_intent_id,
            })
        self.account_payment_id = canonical_payment.id
        return payments

    # ------------------------------------------------------------------
    # Cancel / refund
    # ------------------------------------------------------------------
    def action_cancel(self):
        self.ensure_one()
        if self.status not in ("draft", "processing", "requires_action"):
            raise UserError(_("Only an in-progress payment can be canceled."))
        stripe, dummy = stripe_client.get_stripe_for_company(
            self.env, self.company_id)
        # Best-effort: stop the reader, then cancel the intent.
        if self.reader_id.stripe_reader_id:
            try:
                stripe.terminal.Reader.cancel_action(self.reader_id.stripe_reader_id)
            except Exception as ex:  # pragma: no cover
                _logger.info("Reader cancel_action skipped: %s", ex)
        if self.payment_intent_id:
            try:
                stripe.PaymentIntent.cancel(self.payment_intent_id)
            except Exception as ex:
                _logger.exception("PaymentIntent cancel failed")
                raise UserError(_("Could not cancel the payment: %s", ex))
        self._cleanup_unposted_terminal_surcharge()
        self.write({"status": "canceled"})
        return True

    def action_refund(self):
        """Open the refund wizard (full or partial) for a succeeded payment."""
        self.ensure_one()
        if self.status != "succeeded":
            raise UserError(_("Only a succeeded payment can be refunded."))
        if self.currency_id.compare_amounts(
            self._get_refundable_base_amount(),
            0,
        ) <= 0:
            raise UserError(_(
                "This payment has no unreserved amount available to refund."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Refund Payment"),
            "res_model": "stripe.terminal.refund.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_payment_id": self.id},
        }

    def _get_active_refunds(self):
        self.ensure_one()
        return self.refund_ids.filtered(
            lambda refund: refund.status in _ACTIVE_REFUND_STATUSES
        )

    def _get_refundable_base_amount(self):
        self.ensure_one()
        reserved = sum(self._get_active_refunds().mapped("base_amount"))
        return self.currency_id.round(
            self.amount - self.refunded_amount - reserved
        )

    def _get_terminal_refund_quote(self, amount):
        """Return the base, surcharge net, surcharge gross, and gross refund."""
        self.ensure_one()
        currency = self.currency_id
        amount = currency.round(amount)
        active_refunds = self._get_active_refunds()
        available = self._get_refundable_base_amount()
        remaining_net = currency.round(
            self.surcharge_net_amount
            - self.refunded_surcharge_net_amount
            - sum(active_refunds.mapped("surcharge_net_amount"))
        )
        if not currency.compare_amounts(amount, available):
            surcharge_net = remaining_net
        elif self.amount:
            surcharge_net = currency.round(
                self.surcharge_net_amount * amount / self.amount
            )
        else:
            surcharge_net = 0.0

        surcharge_amount = 0.0
        if currency.compare_amounts(surcharge_net, 0) > 0:
            source_line = self.env["account.move.line"]
            taxes = self.env["account.tax"]
            product = self.env["product.product"]
            partner = self.partner_id
            if self.fee_move_id:
                source_line = self.fee_move_id.invoice_line_ids[:1]
                taxes = source_line.tax_ids
                product = source_line.product_id
                partner = self.fee_move_id.partner_shipping_id or self.partner_id
            elif self.surcharge_sale_order_line_id:
                source_line = self.surcharge_sale_order_line_id
                taxes = source_line.tax_id
                product = source_line.product_id
                partner = (
                    source_line.order_id.partner_shipping_id
                    or self.partner_id
                )
            if source_line:
                tax_values = taxes.compute_all(
                    surcharge_net,
                    currency=currency,
                    quantity=1.0,
                    product=product,
                    partner=partner,
                )
                surcharge_amount = currency.round(
                    tax_values["total_included"]
                )
            elif self.amount:
                surcharge_amount = currency.round(
                    self.fee_amount * amount / self.amount
                )
        return {
            "base_amount": amount,
            "surcharge_net_amount": surcharge_net,
            "surcharge_amount": surcharge_amount,
            "gross_amount": currency.round(amount + surcharge_amount),
        }

    def _process_refund(self, amount):
        """Create a Stripe refund and book it only after Stripe succeeds."""
        self.ensure_one()
        if self.status != "succeeded":
            raise UserError(_("Only a succeeded payment can be refunded."))
        self.env.cr.execute(
            'SELECT id FROM "stripe_terminal_payment" WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset([
            "status",
            "refunded_amount",
            "refunded_surcharge_net_amount",
            "refunded_surcharge_amount",
            "refund_ids",
        ])
        currency = self.currency_id
        amount = currency.round(amount)
        available = self._get_refundable_base_amount()
        if currency.compare_amounts(amount, 0) <= 0:
            raise UserError(_("The refund amount must be positive."))
        if currency.compare_amounts(amount, available) > 0:
            raise UserError(_(
                "The refund amount cannot exceed the refundable balance of "
                "%(amount)s %(currency)s.",
                amount=available, currency=currency.name))
        quote = self._get_terminal_refund_quote(amount)
        cumulative_base = currency.round(
            self.refunded_amount
            + sum(self._get_active_refunds().mapped("base_amount"))
            + amount
        )
        idempotency_key = (
            f"terminal_refund_{self.id}_"
            f"{stripe_client.to_stripe_amount(cumulative_base, currency)}"
        )
        refund_record = self.env["stripe.terminal.refund"].sudo().create({
            "payment_id": self.id,
            "idempotency_key": idempotency_key,
            **quote,
        })

        stripe, dummy = stripe_client.get_stripe_for_company(
            self.env, self.company_id)
        try:
            refund = stripe.Refund.create(
                payment_intent=self.payment_intent_id,
                amount=stripe_client.to_stripe_amount(
                    quote["gross_amount"],
                    currency,
                ),
                idempotency_key=idempotency_key,
            )
        except Exception as ex:
            refund_record.unlink()
            _logger.exception("Refund failed")
            raise UserError(_("Stripe refund failed: %s", ex))
        refund_record._apply_stripe_status(refund)
        return refund_record

    def _finalize_terminal_refund(self, refund_record):
        """Post accounting for one Stripe-confirmed refund exactly once."""
        self.ensure_one()
        refund_record.ensure_one()
        if refund_record.status == "succeeded":
            return refund_record
        self.env.cr.execute(
            'SELECT id FROM "stripe_terminal_payment" WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset([
            "refunded_amount",
            "refunded_surcharge_net_amount",
            "refunded_surcharge_amount",
        ])
        provider = self.env["payment.provider"]._get_terminal_provider(
            self.company_id
        )
        refund_move, refund_line = (
            provider._stripe_create_surcharge_refund_document(
                net_amount=refund_record.surcharge_net_amount,
                origin=self.sale_order_id.name,
                reference=_(
                    "Stripe Terminal surcharge refund for %s",
                    self.name,
                ),
                source_invoice=self.fee_move_id,
                source_sale_line=self.surcharge_sale_order_line_id,
            )
            if refund_record.surcharge_net_amount
            else (
                self.env["account.move"],
                self.env["sale.order.line"],
            )
        )
        surcharge_refund = (
            refund_move.amount_total
            if refund_move
            else abs(refund_line.price_total)
            if refund_line
            else 0.0
        )
        if self.currency_id.compare_amounts(
            surcharge_refund,
            refund_record.surcharge_amount,
        ):
            if refund_move:
                refund_move.unlink()
            if refund_line:
                refund_line.unlink()
            refund_record.write({
                "status": "manual_review",
                "error_message": _(
                    "The local surcharge credit is %(actual)s, but Stripe "
                    "refunded %(expected)s. Accounting review is required.",
                    actual=self.currency_id.format(surcharge_refund),
                    expected=self.currency_id.format(
                        refund_record.surcharge_amount
                    ),
                ),
            })
            self.message_post(body=refund_record.error_message)
            return refund_record
        if refund_move:
            refund_move.action_post()
        refund_payment = self._refund_account_payment(
            refund_record.gross_amount,
            surcharge_refund_move=refund_move,
        )
        total_refunded = self.currency_id.round(
            self.refunded_amount + refund_record.base_amount
        )
        total_surcharge_refunded = self.currency_id.round(
            self.refunded_surcharge_amount
            + refund_record.surcharge_amount
        )
        total_net_refunded = self.currency_id.round(
            self.refunded_surcharge_net_amount
            + refund_record.surcharge_net_amount
        )
        fully_refunded = self.currency_id.compare_amounts(
            total_refunded,
            self.amount,
        ) >= 0
        refund_record.write({
            "status": "succeeded",
            "account_payment_id": refund_payment.id,
            "surcharge_refund_move_id": refund_move.id,
            "surcharge_refund_sale_line_id": refund_line.id,
            "error_message": False,
        })
        self.write({
            "refunded_amount": total_refunded,
            "refunded_surcharge_amount": total_surcharge_refunded,
            "refunded_surcharge_net_amount": total_net_refunded,
            "surcharge_refund_move_ids": (
                [(4, refund_move.id)] if refund_move else []
            ),
            "surcharge_refund_sale_line_ids": (
                [(4, refund_line.id)] if refund_line else []
            ),
            "status": "refunded" if fully_refunded else self.status,
        })
        self.message_post(body=_(
            "Refund issued: %(base)s base plus %(surcharge)s surcharge "
            "(%(total)s total). Accounting payment: %(payment)s.",
            base=self.currency_id.format(refund_record.base_amount),
            surcharge=self.currency_id.format(refund_record.surcharge_amount),
            total=self.currency_id.format(refund_record.gross_amount),
            payment=refund_payment.display_name or _("not created"),
        ))
        return refund_record

    def _refund_account_payment(self, amount, surcharge_refund_move=False):
        """Settle the refund in Odoo, reproducing Odoo's native refund booking.

        Odoo's own ``payment.refund.wizard`` cannot be reused here: it delegates
        to ``payment.transaction.action_refund`` (which both requires a
        ``payment.transaction`` — terminal payments have none — and would fire a
        *second* Stripe refund). Instead we mirror exactly what that flow ends up
        posting: an outbound customer payment reconciled against the original
        payment, so the customer balance nets out and any invoice the original
        payment had settled re-opens by the refunded amount.

        Best-effort: the Stripe refund has already gone through, so a settlement
        failure is logged and noted on the chatter rather than raised, leaving
        accounting to reconcile manually.
        """
        self.ensure_one()
        source = self.account_payment_id.sudo()
        if not source or self.currency_id.is_zero(amount):
            return self.env["account.payment"]
        refund = self.env["account.payment"].sudo().with_company(
            source.company_id
        ).create({
            "payment_type": "outbound",
            "partner_type": "customer",
            "partner_id": source.partner_id.id,
            "amount": amount,
            "currency_id": source.currency_id.id,
            "journal_id": source.journal_id.id,
            "company_id": source.company_id.id,
            "sale_order_id": source.sale_order_id.id,
            "memo": _("Refund of %s", source.memo or self.name),
        })
        refund.action_post()
        try:
            self._settle_refund_payment(
                source,
                refund,
                surcharge_refund_move=surcharge_refund_move,
            )
        except Exception as ex:  # pragma: no cover - best effort booking
            _logger.warning(
                "Could not reconcile refund %s with payment %s: %s",
                refund.name, source.name, ex)
            self.message_post(body=_(
                "Refund payment %(refund)s was posted but could not be "
                "automatically reconciled against %(source)s. Please settle it "
                "manually.", refund=refund.name, source=source.name))
        return refund

    def _settle_refund_payment(
        self,
        source,
        refund,
        *,
        surcharge_refund_move=False,
    ):
        """Reconcile ``refund`` against ``source`` on the receivable account.

        If the source payment was reconciled against an invoice, its existing
        reconciliation is broken first and the invoice counterpart is included so
        Odoo re-opens the invoice by the refunded amount; for a standalone
        payment the two simply net out.
        """
        def receivable(line):
            return line.account_id.account_type in (
                "asset_receivable", "liability_payable")

        source_line = source.move_id.line_ids.filtered(receivable)
        refund_line = refund.move_id.line_ids.filtered(receivable)
        if not source_line or not refund_line:
            return
        if surcharge_refund_move:
            refund_surcharge_lines = surcharge_refund_move.line_ids.filtered(
                lambda line: (
                    line.display_type == "payment_term"
                    and line.account_id == refund_line.account_id
                    and not line.reconciled
                )
            )
            if refund_surcharge_lines:
                (refund_line + refund_surcharge_lines).reconcile()
        # Invoice (or other) lines currently matched with the source payment.
        counterpart = (
            source_line.matched_debit_ids.debit_move_id
            | source_line.matched_credit_ids.credit_move_id)
        if source_line.matched_debit_ids or source_line.matched_credit_ids:
            source_line.remove_move_reconcile()
        surcharge_invoice = (
            self.fee_move_id
            or self.account_payment_id.stripe_surcharge_invoice_id
        )
        if not surcharge_invoice and self.surcharge_sale_order_line_id:
            surcharge_invoice = (
                self.surcharge_sale_order_line_id.invoice_lines.move_id
                .filtered(lambda move: move.state == "posted")[:1]
            )
        surcharge_lines = counterpart.filtered(
            lambda line: line.move_id == surcharge_invoice
        )
        base_counterpart = counterpart - surcharge_lines
        if surcharge_lines:
            (source_line + surcharge_lines).filtered(
                lambda line: not line.reconciled
            ).reconcile()
        (source_line + refund_line + base_counterpart).filtered(
            lambda line: not line.reconciled
        ).reconcile()

    # ------------------------------------------------------------------
    # Cron safety net
    # ------------------------------------------------------------------
    @api.model
    def _cron_poll_processing(self):
        """Poll Stripe for payments stuck in processing (no webhook configured)."""
        pending = self.search([
            ("status", "in", ("processing", "requires_action")),
            ("payment_intent_id", "!=", False),
        ])
        for payment in pending:
            try:
                payment.action_check_status()
            except Exception as ex:  # pragma: no cover
                _logger.warning("Polling %s failed: %s", payment.name, ex)
        refunds = self.env["stripe.terminal.refund"].search([
            ("status", "in", ("pending", "requires_action")),
            ("stripe_refund_id", "!=", False),
        ])
        for refund in refunds:
            try:
                stripe, dummy = stripe_client.get_stripe_for_company(
                    self.env,
                    refund.company_id,
                )
                refund._apply_stripe_status(
                    stripe.Refund.retrieve(refund.stripe_refund_id)
                )
            except Exception as ex:  # pragma: no cover
                _logger.warning(
                    "Polling refund %s failed: %s",
                    refund.stripe_refund_id,
                    ex,
                )
        return True


class StripeTerminalRefund(models.Model):
    _name = "stripe.terminal.refund"
    _description = "Stripe Terminal Refund"
    _order = "create_date desc, id desc"

    _stripe_refund_unique = models.Constraint(
        "unique(stripe_refund_id)",
        "A Stripe refund can only be linked to one terminal refund.",
    )
    _idempotency_key_unique = models.Constraint(
        "unique(idempotency_key)",
        "A terminal refund idempotency key can only be used once.",
    )

    payment_id = fields.Many2one(
        "stripe.terminal.payment",
        required=True,
        ondelete="cascade",
        index=True,
    )
    stripe_refund_id = fields.Char(
        string="Stripe Refund",
        readonly=True,
        copy=False,
        index=True,
    )
    idempotency_key = fields.Char(required=True, readonly=True, copy=False)
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("requires_action", "Requires Action"),
            ("succeeded", "Succeeded"),
            ("failed", "Failed"),
            ("canceled", "Canceled"),
            ("manual_review", "Accounting Review"),
        ],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    base_amount = fields.Monetary(required=True, readonly=True)
    surcharge_net_amount = fields.Monetary(readonly=True)
    surcharge_amount = fields.Monetary(readonly=True)
    gross_amount = fields.Monetary(required=True, readonly=True)
    currency_id = fields.Many2one(
        related="payment_id.currency_id",
        store=True,
    )
    company_id = fields.Many2one(
        related="payment_id.company_id",
        store=True,
        index=True,
    )
    account_payment_id = fields.Many2one(
        "account.payment",
        string="Accounting Refund",
        readonly=True,
        copy=False,
    )
    surcharge_refund_move_id = fields.Many2one(
        "account.move",
        string="Surcharge Credit Note",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    surcharge_refund_sale_line_id = fields.Many2one(
        "sale.order.line",
        string="Surcharge Refund Order Line",
        readonly=True,
        copy=False,
    )
    error_message = fields.Char(readonly=True, copy=False)

    def _apply_stripe_status(self, refund_data):
        """Apply a Stripe refund lifecycle event idempotently."""
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM "stripe_terminal_refund" WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset([
            "status",
            "stripe_refund_id",
            "account_payment_id",
        ])
        if self.status == "succeeded":
            return self
        stripe_refund_id = stripe_client.sget(refund_data, "id")
        stripe_status = stripe_client.sget(refund_data, "status") or "pending"
        refunded = stripe_client.from_stripe_amount(
            stripe_client.sget(refund_data, "amount") or 0,
            self.currency_id,
        )
        if self.currency_id.compare_amounts(refunded, self.gross_amount):
            self.write({
                "stripe_refund_id": stripe_refund_id or self.stripe_refund_id,
                "status": "manual_review",
                "error_message": _(
                    "Stripe reports %(actual)s refunded, but %(expected)s was "
                    "requested. Accounting review is required.",
                    actual=self.currency_id.format(refunded),
                    expected=self.currency_id.format(self.gross_amount),
                ),
            })
            self.payment_id.message_post(body=self.error_message)
            return self
        values = {
            "stripe_refund_id": stripe_refund_id or self.stripe_refund_id,
            "status": (
                stripe_status
                if stripe_status in dict(self._fields["status"].selection)
                else "pending"
            ),
            "error_message": (
                stripe_client.sget(refund_data, "failure_reason")
                or stripe_client.sget(refund_data, "pending_reason")
                or False
            ),
        }
        if stripe_status == "succeeded":
            self.write({
                **values,
                "status": "pending",
            })
            return self.payment_id._finalize_terminal_refund(self)
        self.write(values)
        if stripe_status in ("failed", "canceled"):
            self.payment_id.message_post(body=_(
                "Stripe refund %(refund)s %(status)s. No local refund "
                "accounting was posted.%(reason)s",
                refund=self.stripe_refund_id or self.id,
                status=stripe_status,
                reason=(
                    f" {self.error_message}"
                    if self.error_message
                    else ""
                ),
            ))
        else:
            self.payment_id.message_post(body=_(
                "Stripe refund %(refund)s is %(status)s. Accounting will be "
                "posted only after Stripe confirms success.",
                refund=self.stripe_refund_id or self.id,
                status=self.status,
            ))
        return self
