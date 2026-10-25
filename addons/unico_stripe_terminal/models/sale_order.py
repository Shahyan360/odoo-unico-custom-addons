from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Payment states that mean the money has actually been collected. In Odoo 19 an
# account.payment often has NO journal entry (``move_id`` is empty) until it is
# matched to a bank statement, so settlement must be read from the payment
# ``state`` itself — "in_process" (captured / registered, awaiting bank match)
# and "paid" (fully reconciled) both count; draft/canceled/rejected do not.
COLLECTED_PAYMENT_STATES = ("in_process", "paid")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Terminal (gateway) records - kept for managing card transactions.
    stripe_terminal_payment_ids = fields.One2many(
        comodel_name="stripe.terminal.payment",
        inverse_name="sale_order_id",
        string="Terminal Payments",
    )
    stripe_terminal_payment_count = fields.Integer(
        string="Payment Count",
        compute="_compute_stripe_terminal_payment_count",
        compute_sudo=True,
    )
    stripe_terminal_amount_collected = fields.Monetary(
        string="Amount Collected",
        compute="_compute_stripe_terminal_amounts",
        compute_sudo=True,
        help="Total of posted customer payments (cash or card) collected on this order.",
    )
    stripe_terminal_amount_due = fields.Monetary(
        string="Amount Due",
        compute="_compute_stripe_terminal_amounts",
        compute_sudo=True,
        help="Order total minus the amount already collected. The payment "
             "button is hidden once this reaches zero.",
    )
    # Online payment transactions (ACH / card) still awaiting clearing. They are
    # not yet collected money, so they do not count toward Amount Collected, but
    # the order should surface them (ribbon + smart button) so they can be chased
    # or manually confirmed once the bank confirms the funds.
    pending_payment_transaction_ids = fields.Many2many(
        comodel_name="payment.transaction",
        string="Pending Payment Transactions",
        compute="_compute_pending_payment_transactions",
        compute_sudo=True,
    )
    pending_payment_transaction_count = fields.Integer(
        string="Pending Payment Count",
        compute="_compute_pending_payment_transactions",
        compute_sudo=True,
    )
    has_pending_payment_transaction = fields.Boolean(
        string="Has Pending Payment",
        compute="_compute_pending_payment_transactions",
        compute_sudo=True,
    )

    def _stripe_terminal_can_view_account_payments(self):
        return self.env.user.has_groups(
            "account.group_account_invoice,account.group_account_readonly")

    def _stripe_terminal_check_collection_access(self):
        if self.env.su:
            return
        if not self.env.user.has_group(
                "unico_stripe_terminal.group_stripe_terminal_user"):
            raise AccessError(_(
                "Only Stripe Terminal users can collect payments from a sales order."))

    def _unico_customer_payments_map(self):
        """Map each order id -> the inbound, posted customer payments collected
        for it, regardless of how they were taken:

        * terminal / cash payments recorded through Collect Payment (linked to
          the order via ``sale_order_id`` -> ``sale_payment_ids``),
        * payments reconciled against the order's customer invoices — manual
          ``account.payment`` records and online/portal payments — which carry
          no ``sale_order_id`` and were previously invisible to the order, and
        * payments created by the order's online/portal payment transactions
          (``transaction_ids`` in state ``done`` -> ``payment_id``). These carry
          no ``sale_order_id`` and, when the order is paid before any invoice is
          posted, reconcile against no invoice either, so the two sources above
          both miss them — leaving the count at 0 with no smart button/ribbon.

        Returns a dict so the (non-stored) amount/count computes and the smart
        button all share one batched lookup. Recordset unions de-duplicate the
        overlap (a terminal payment reconciled against an invoice appears in both
        sources but is counted once).
        """
        Payment = self.env["account.payment"].sudo()
        orders = self.sudo()
        result = {order.id: Payment.browse() for order in orders}

        # 1. Terminal / cash payments tagged with the order.
        for order in orders:
            result[order.id] |= order.sale_payment_ids

        # 2. Payments reconciled against the orders' posted customer invoices.
        invoices = orders.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted")
        if invoices:
            inv_to_orders = {}
            for order in orders:
                for inv in order.invoice_ids:
                    inv_to_orders.setdefault(inv.id, orders.browse())
                    inv_to_orders[inv.id] |= order
            for payment in Payment.search([("reconciled_invoice_ids", "in", invoices.ids)]):
                for inv in payment.reconciled_invoice_ids:
                    for order in inv_to_orders.get(inv.id, orders.browse()):
                        result[order.id] |= payment

        # 3. Payments created by the order's online/portal payment transactions.
        for order in orders:
            for tx in order.sudo().transaction_ids.filtered(
                    lambda t: t.state == "done" and t.payment_id):
                result[order.id] |= tx.payment_id

        # Keep only collected inbound customer payments.
        for oid, payments in result.items():
            result[oid] = payments.filtered(
                lambda p: p.payment_type == "inbound"
                and p.state in COLLECTED_PAYMENT_STATES)
        return result

    @api.depends("sale_payment_ids", "sale_payment_ids.state",
                 "invoice_ids.payment_state", "invoice_ids.state",
                 "transaction_ids.state", "transaction_ids.payment_id")
    def _compute_stripe_terminal_payment_count(self):
        payments_map = self._unico_customer_payments_map()
        for order in self:
            order.stripe_terminal_payment_count = len(payments_map.get(order.id))

    @api.depends("amount_total", "sale_payment_ids.amount",
                 "sale_payment_ids.payment_type", "sale_payment_ids.state",
                 "invoice_ids.payment_state", "invoice_ids.amount_residual",
                 "invoice_ids.state",
                 "transaction_ids.state", "transaction_ids.payment_id")
    def _compute_stripe_terminal_amounts(self):
        payments_map = self._unico_customer_payments_map()
        for order in self:
            collected = sum(payments_map.get(order.id).mapped("amount"))
            order.stripe_terminal_amount_collected = collected
            order.stripe_terminal_amount_due = max(order.amount_total - collected, 0.0)

    @api.depends("transaction_ids.state")
    def _compute_pending_payment_transactions(self):
        for order in self:
            pending = order.sudo().transaction_ids.filtered(
                lambda t: t.state == "pending")
            order.pending_payment_transaction_ids = pending
            order.pending_payment_transaction_count = len(pending)
            order.has_pending_payment_transaction = bool(pending)

    def action_view_pending_payment_transactions(self):
        """Open the order's pending (uncleared) online payment transactions."""
        self.ensure_one()
        transactions = self.pending_payment_transaction_ids
        action = {
            "type": "ir.actions.act_window",
            "name": _("Pending Payments"),
            "res_model": "payment.transaction",
            "context": {"create": False},
        }
        if len(transactions) == 1:
            action.update(view_mode="form", res_id=transactions.id)
        else:
            action.update(
                view_mode="list,form",
                domain=[("id", "in", transactions.ids)],
            )
        return action

    def _stripe_terminal_open_invoices(self):
        """Posted customer invoices of the order that still have an amount due."""
        self.ensure_one()
        return self.sudo().invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice"
            and m.state == "posted"
            and m.payment_state in ("not_paid", "partial")
            and m.amount_residual > 0
        ).sorted(lambda m: m.invoice_date_due or m.invoice_date or m.id)

    def _stripe_register_customer_payment(
        self,
        amount,
        journal,
        memo,
        surcharge_invoice=False,
        payment_method_line=False,
    ):
        """Post an inbound customer payment for ``amount`` in ``journal``.

        Reconciles against the order's open invoices when there are any
        (Odoo marks them Paid), otherwise records a standalone payment. The
        payment is tagged with the order so it counts toward Amount Collected.
        ``payment_method_line`` records how the money came in (e.g. the
        journal's Cash line); the journal's default is used when omitted.
        Returns the created ``account.payment`` record(s).
        """
        self.ensure_one()
        self._stripe_terminal_check_collection_access()
        invoices = self._stripe_terminal_open_invoices()
        if surcharge_invoice:
            if surcharge_invoice.state == "draft":
                surcharge_invoice.action_post()
            invoices |= surcharge_invoice
        AccountPayment = self.env["account.payment"].sudo().with_company(self.company_id)
        if invoices:
            wizard = self.env["account.payment.register"].sudo().with_company(
                self.company_id
            ).with_context(
                active_model="account.move",
                active_ids=invoices.ids,
                skip_stripe_surcharge=True,
            ).create({
                "journal_id": journal.id,
                "amount": amount,
                "payment_date": fields.Date.context_today(self),
                "communication": memo,
                "group_payment": True,
                **({"payment_method_line_id": payment_method_line.id}
                   if payment_method_line else {}),
            })
            payments = wizard._create_payments()
        else:
            payments = AccountPayment.create({
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner_id.id,
                "amount": amount,
                "currency_id": self.currency_id.id,
                "journal_id": journal.id,
                "company_id": self.company_id.id,
                "memo": memo,
                **({"payment_method_line_id": payment_method_line.id}
                   if payment_method_line else {}),
            })
            payments.action_post()
        payments.write({"sale_order_id": self.id})
        if surcharge_invoice:
            provider = self.env["payment.provider"]._get_terminal_provider(
                self.company_id
            )
            base_invoices = invoices - surcharge_invoice
            for payment in payments:
                payment.stripe_surcharge_invoice_id = surcharge_invoice
                provider._stripe_reconcile_surcharge_first(
                    payment,
                    surcharge_invoice,
                    base_invoices,
                )
        return payments

    def action_collect_stripe_terminal_payment(self):
        """Open the Collect Payment wizard (cash or Stripe Terminal).

        An invoice is not required: if the order has an open posted invoice the
        payment is reconciled against it, otherwise it is recorded as a
        standalone customer payment on the order.
        """
        self.ensure_one()
        if self.currency_id.compare_amounts(self.stripe_terminal_amount_due, 0) <= 0:
            raise UserError(_("This order has nothing left to collect."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Collect Payment"),
            "res_model": "stripe.terminal.payment.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_sale_order_id": self.id,
                "default_partner_id": self.partner_id.id,
                "default_currency_id": self.currency_id.id,
                "default_amount": self.stripe_terminal_amount_due,
            },
        }

    def action_view_stripe_terminal_payments(self):
        self.ensure_one()
        if not self._stripe_terminal_can_view_account_payments():
            return {
                "type": "ir.actions.act_window",
                "name": _("Terminal Payments"),
                "res_model": "stripe.terminal.payment",
                "view_mode": "list,form",
                "domain": [("sale_order_id", "=", self.id)],
                "context": {"default_sale_order_id": self.id},
            }
        payments = self._unico_customer_payments_map()[self.id]
        return {
            "type": "ir.actions.act_window",
            "name": _("Payments"),
            "res_model": "account.payment",
            "view_mode": "list,form",
            "domain": [("id", "in", payments.ids)],
            "context": {"default_sale_order_id": self.id},
        }


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    stripe_terminal_payment_id = fields.Many2one(
        "stripe.terminal.payment",
        string="Stripe Terminal Payment",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
