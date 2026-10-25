from datetime import timedelta

from odoo import api, fields, models

FULFILLMENT_TYPES = [
    ("delivery", "Delivery"),
    ("will_call", "Will Call/Customer Pickup"),
]


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def unlink(self):
        """Run special-line RFQ cleanup before native order cascades the lines."""
        # Core blocks deletion once a quotation is sent or confirmed.  Let that
        # validation run before deleting any child line or draft RFQ so callers
        # that catch the UserError cannot retain partial cleanup side effects.
        if any(order.state not in ("draft", "cancel") for order in self):
            return super().unlink()

        order_lines = self.sudo().mapped("order_line")
        if hasattr(order_lines, "_assert_cpq_custom_door_purchase_delete_allowed"):
            order_lines._assert_cpq_custom_door_purchase_delete_allowed()
        special_lines = order_lines.filtered(
            lambda line: line._unico_linked_special_purchases()
        )
        if special_lines:
            special_lines.unlink()
        return super().unlink()

    fulfillment_type = fields.Selection(
        selection=FULFILLMENT_TYPES,
        string="Fulfillment Type",
        default="will_call",
        copy=True,
        help="How the customer receives the goods of this order. "
             "Used to split the delivery orders dashboard card.",
    )

    rma_credit_note_ids = fields.One2many(
        comodel_name="account.move",
        inverse_name="sale_id",
        string="RMA Credit Notes",
        domain=[("move_type", "=", "out_refund")],
        help="Customer credit notes raised for this order's RMA return. "
             "Drives the Returned/Credited payment status and lets the stored "
             "status refresh when such a credit note is posted.",
    )

    unico_payment_status = fields.Selection(
        selection=[
            ("not_paid", "Unpaid"),
            ("paid", "Paid"),
            ("returned_credited", "Returned/Credited"),
        ],
        string="Payment Status",
        compute="_compute_unico_payment_status",
        compute_sudo=True,
        store=True,
        help="Returned/Credited when the order's own sale was returned and "
             "fully credited back through an RMA credit note (regardless of how "
             "it was originally paid). Otherwise Paid once any collected customer "
             "payment is found, or the order's invoices are fully covered by an "
             "applied credit note; unpaid otherwise.",
    )

    @api.depends(
        "state",
        "amount_total",
        "sale_payment_ids",
        "sale_payment_ids.state",
        "sale_payment_ids.payment_type",
        # Manual account.payment records and online/portal payments reconcile
        # against the invoice, or link only through the payment transaction,
        # rather than carrying a sale_order_id — so the stored status must also
        # refresh when an invoice's settlement or a payment transaction changes.
        "invoice_ids.payment_state",
        "invoice_ids.state",
        "invoice_ids.amount_residual",
        "transaction_ids.state",
        "transaction_ids.payment_id",
        # An RMA return raises an out_refund credit note carrying this order's
        # sale_id — refresh the status when one is posted/changed so a returned
        # order flips to Returned/Credited.
        "rma_credit_note_ids.state",
        "rma_credit_note_ids.amount_total",
        "rma_credit_note_ids.rma_id",
    )
    def _compute_unico_payment_status(self):
        # Collected-payments source shared with the Payments count/amount
        # (_compute_stripe_terminal_payment_count): terminal & cash, manual
        # account.payment, and online/portal transaction payments. RMA credit
        # detection keys off the credit note (which carries sale_id/rma_id
        # whatever the original payment method), so it covers terminal, online
        # and manual account.payment orders alike.
        payments_map = self._unico_customer_payments_map()
        credit_map = self._unico_rma_credit_map()
        for order in self:
            # Payment status only applies to confirmed orders. Quotations and
            # cancelled orders carry no status (they group under "None" and are
            # excluded from the Paid/Unpaid dashboard tiles), so the dashboard
            # counts and the "Payment Status" group-by always agree — otherwise a
            # prepaid quotation would show as Paid in the group-by but not on the
            # state='sale'-scoped dashboard.
            if order.state != "sale":
                order.unico_payment_status = False
                continue
            credited = credit_map.get(order.id, 0.0)
            if credited and order.currency_id.compare_amounts(
                    credited, order.amount_total) >= 0:
                # The order's own sale was returned and fully credited back
                # through an RMA credit note: surface that rather than "Paid",
                # whatever the original payment method.
                order.unico_payment_status = "returned_credited"
            elif payments_map.get(order.id):
                # Paid as soon as any collected (in_process/paid) inbound
                # customer payment is found — even one that only covers it
                # partially.
                order.unico_payment_status = "paid"
            elif order._unico_invoices_fully_settled():
                # No collected customer payment, but the invoices carry no
                # residual (e.g. settled by an applied credit note). Counts as
                # paid only when fully covered; a partial credit stays unpaid.
                order.unico_payment_status = "paid"
            else:
                order.unico_payment_status = "not_paid"

    def _unico_rma_credit_map(self):
        """Map order id -> total posted RMA credit-note value raised *for* that
        order (``out_refund`` moves carrying the order's ``rma_id`` and
        ``sale_id``). Independent of how the order was originally paid, so
        terminal, online and manual ``account.payment`` orders are all covered.
        """
        result = {order.id: 0.0 for order in self}
        if not self:
            return result
        Move = self.env["account.move"].sudo()
        for sale, total in Move._read_group(
            [
                ("move_type", "=", "out_refund"),
                ("state", "=", "posted"),
                ("rma_id", "!=", False),
                ("sale_id", "in", self.ids),
            ],
            groupby=["sale_id"],
            aggregates=["amount_total:sum"],
        ):
            if sale:
                result[sale.id] = total
        return result

    def _unico_invoices_fully_settled(self):
        """True when the order has posted customer invoices and every one of
        them is fully settled (no residual) — e.g. covered by an applied credit
        note rather than a collected payment. Lets a fully credit-covered order
        read as Paid while a partially covered one does not."""
        self.ensure_one()
        invoices = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted")
        if not invoices:
            return False
        return all(
            m.payment_state in ("paid", "in_payment", "reversed")
            for m in invoices)

    @api.model
    def retrieve_dashboard_unico(self):
        """Counts used by the Sales list-view dashboard banner. Each count uses
        the same domain as its matching (invisible) search filter so that a tile
        and the list it opens always stay consistent."""
        SO = self.env["sale.order"]
        return {
            "delivery": SO.search_count([("fulfillment_type", "=", "delivery")]),
            "will_call": SO.search_count([("fulfillment_type", "=", "will_call")]),
            "quotation": SO.search_count([("state", "in", ("draft", "sent"))]),
            "sale_order": SO.search_count([("state", "=", "sale")]),
            "unpaid": SO.search_count(
                [("state", "=", "sale"), ("unico_payment_status", "=", "not_paid")]
            ),
            "paid": SO.search_count(
                [("state", "=", "sale"), ("unico_payment_status", "=", "paid")]
            ),
            "returned_credited": SO.search_count(
                [("state", "=", "sale"),
                 ("unico_payment_status", "=", "returned_credited")]
            ),
        }

    # ------------------------------------------------------------------
    # Delivery Date (commitment_date): left empty on the quotation, filled from
    # the customer lead time on confirmation, manually settable by Sales
    # Managers only.
    # ------------------------------------------------------------------
    unico_auto_commitment_date = fields.Datetime(
        string="Auto Delivery Date",
        copy=False,
        help="Delivery Date automatically derived from the order's customer lead "
             "time on confirmation. Empty when the date was entered by hand.",
    )
    unico_delivery_date_readonly = fields.Boolean(
        string="Delivery Date Locked",
        compute="_compute_unico_delivery_date_readonly",
        help="True for users who may not change the Delivery Date. It is locked "
             "for everyone except Sales Managers, who can set or clear it by "
             "hand instead of letting confirmation derive it from the order's "
             "customer lead time.",
    )

    @api.depends_context("uid")
    def _compute_unico_delivery_date_readonly(self):
        is_manager = self.env.user.has_group("sales_team.group_sale_manager")
        for order in self:
            order.unico_delivery_date_readonly = not is_manager

    def _get_max_customer_lead_time(self):
        """Return the highest customer lead time (in days) across the order lines.

        Single/double door CPQ lines contribute a 1-day lead (see
        ``sale.order.line._unico_customer_lead_time``); every other line uses the
        product's Customer Lead Time (``sale_delay``). Lines with no lead time
        (0 days) are ignored."""
        self.ensure_one()
        delays = []
        for line in self.order_line:
            if not line.product_id:
                continue
            lead = line._unico_customer_lead_time()
            if lead > 0:
                delays.append(lead)
        return max(delays) if delays else 0

    def _apply_customer_lead_time(self):
        """Fill an empty Delivery Date with today + the maximum customer lead
        time of the order lines, when at least one product carries a lead time.

        Only ever fills a *blank* Delivery Date. A quotation starts with no
        Delivery Date and nothing sets one while it is being edited, so whatever
        a user types (or leaves empty) is what the quotation carries. The
        lead-time automation then runs once, on confirmation, for orders that
        were left without a date."""
        for order in self:
            if order.commitment_date:
                continue
            max_delay = order._get_max_customer_lead_time()
            if max_delay > 0:
                target = fields.Datetime.now() + timedelta(days=max_delay)
                order.commitment_date = target
                order.unico_auto_commitment_date = target

    def action_confirm(self):
        # The Delivery Date is deliberately not derived while the order is a
        # quotation: it is set here, on confirmation, and only when the user did
        # not enter one by hand.
        self._apply_customer_lead_time()
        return super().action_confirm()

    # ------------------------------------------------------------------
    # Automatic customer emails
    # ------------------------------------------------------------------
    # Both of these fire only on the online-payment path: paying on the portal
    # confirms the quotation with ``send_email`` in the context, and a
    # successful transaction mails a receipt. Neither has a switch in core, and
    # neither is reached when a user confirms an order in the backend or clicks
    # Send by Email — so filtering the recordset here leaves manual sending,
    # the Send & Print wizard and chatter untouched.
    def _send_order_confirmation_mail(self):
        return super(
            SaleOrder, self._unico_customer_email_allowed()
        )._send_order_confirmation_mail()

    def _send_payment_succeeded_for_order_mail(self):
        return super(
            SaleOrder, self._unico_customer_email_allowed()
        )._send_payment_succeeded_for_order_mail()

    def _unico_customer_email_allowed(self):
        return self.filtered(
            lambda order: order.company_id.unico_send_automatic_customer_emails
        )

    def action_unico_regenerate_pickings(self):
        """Compatibility action for stale deployed sale-order views.

        The current fulfillment module no longer defines a destructive
        regenerate-pickings workflow, but some databases may still have an
        inherited button with this old method name during module upgrade.
        Keeping this object action available lets view validation pass and
        opens the related delivery orders if the old button is clicked.
        """
        action = self.env["ir.actions.actions"]._for_xml_id(
            "stock.action_picking_tree_all"
        )
        pickings = self.mapped("picking_ids")
        action["domain"] = (
            [("id", "in", pickings.ids)]
            if pickings
            else [("sale_id", "in", self.ids)]
        )
        return action
