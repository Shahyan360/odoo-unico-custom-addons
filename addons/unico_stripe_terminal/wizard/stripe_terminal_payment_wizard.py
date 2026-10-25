from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StripeTerminalPaymentWizard(models.TransientModel):
    _name = "stripe.terminal.payment.wizard"
    _description = "Collect Payment"

    payment_method = fields.Selection(
        selection=[
            ("stripe_terminal", "Stripe Terminal"),
            ("cash", "Cash"),
        ],
        string="Payment Method",
        default="stripe_terminal",
        required=True,
    )
    sale_order_id = fields.Many2one(
        comodel_name="sale.order",
        string="Sales Order",
        required=True,
    )
    partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Customer",
        readonly=True,
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        related="sale_order_id.company_id",
    )
    amount = fields.Monetary(string="Amount", required=True)
    currency_id = fields.Many2one(
        comodel_name="res.currency",
        string="Currency",
        required=True,
    )
    fee_amount = fields.Monetary(
        string="Potential Credit Card Surcharge",
        compute="_compute_fee_amount",
        help=(
            "The configured surcharge applies only after Stripe identifies the "
            "presented card as credit."
        ),
    )
    total_charge = fields.Monetary(
        string="Maximum Card Total",
        compute="_compute_fee_amount",
        help=(
            "Credit-card total including the configured surcharge. Debit, "
            "prepaid, and unknown cards are charged only the base amount."
        ),
    )
    location_id = fields.Many2one(
        comodel_name="stripe.terminal.location",
        string="Location",
        domain="[('company_id', '=', company_id)]",
    )
    reader_id = fields.Many2one(
        comodel_name="stripe.terminal.reader",
        string="Reader",
        domain="['&', ('company_id', '=', company_id),"
               " '|', ('location_id', '=', location_id), ('location_id', '=', False)]",
    )

    @api.depends("amount", "partner_id", "currency_id", "company_id", "payment_method")
    def _compute_fee_amount(self):
        Provider = self.env["payment.provider"].sudo()
        for wizard in self:
            fee = 0.0
            company = wizard.company_id or self.env.company
            provider = Provider.search([
                ("code", "=", "stripe"),
                ("company_id", "=", company.id),
                ("state", "in", ("enabled", "test")),
            ], limit=1)
            if (wizard.payment_method == "stripe_terminal" and provider
                    and getattr(provider, "fees_active", False) and wizard.amount):
                country = wizard.partner_id.country_id or company.country_id
                fee = provider._card_compute_fees(
                    wizard.amount, wizard.currency_id, country)
                fee = wizard.currency_id.round(fee or 0.0)
            wizard.fee_amount = fee
            wizard.total_charge = (wizard.amount or 0.0) + fee

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        company = self.env.company
        provider = self.env["payment.provider"].sudo().search([
            ("code", "=", "stripe"),
            ("company_id", "=", company.id),
            ("state", "in", ("enabled", "test")),
        ], limit=1)
        if not res.get("location_id") and provider.stripe_terminal_default_location_id:
            res["location_id"] = provider.stripe_terminal_default_location_id.id
        # Load the reader from the location's configured default; otherwise
        # fall back to the only online reader if there is just one.
        if not res.get("reader_id"):
            location = self.env["stripe.terminal.location"].browse(res.get("location_id"))
            if location.default_reader_id:
                res["reader_id"] = location.default_reader_id.id
            else:
                readers = self.env["stripe.terminal.reader"].search([
                    ("company_id", "=", company.id), ("status", "=", "online")])
                if len(readers) == 1:
                    res["reader_id"] = readers.id
        return res

    @api.onchange("location_id")
    def _onchange_location_id_default_reader(self):
        """Load the location's default reader when the location changes, unless
        the user already picked a reader that belongs to the new location."""
        for wizard in self:
            location = wizard.location_id
            if not location or not location.default_reader_id:
                continue
            current = wizard.reader_id
            if not current or (current.location_id and current.location_id != location):
                wizard.reader_id = location.default_reader_id

    def action_collect(self):
        self.ensure_one()
        if self.amount <= 0:
            raise UserError(_("The amount to collect must be positive."))
        if self.payment_method == "cash":
            return self._collect_cash()
        if not self.reader_id:
            raise UserError(_("Select a Stripe Terminal reader to charge the card."))
        payment = self.env["stripe.terminal.payment"].create({
            "sale_order_id": self.sale_order_id.id,
            "partner_id": self.partner_id.id,
            "amount": self.amount,
            "currency_id": self.currency_id.id,
            "reader_id": self.reader_id.id,
            "company_id": self.company_id.id,
        })
        payment._process_on_reader()
        return {
            "type": "ir.actions.act_window",
            "name": _("Terminal Payment"),
            "res_model": "stripe.terminal.payment",
            "res_id": payment.id,
            "view_mode": "form",
            "target": "current",
        }

    def _collect_cash(self):
        """Cash is a plain Odoo customer payment (no terminal record).

        The journal and payment method come from the **Cash Journal** / **Cash
        Payment Method** set on the Stripe provider, so cash can share the card
        journal and still be told apart by its payment method.
        """
        order = self.sale_order_id
        if order.state in ("draft", "sent"):
            order.action_confirm()
        journal, method_line = self.env[
            "payment.provider"]._get_cash_settlement_values(order.company_id)
        payments = order._stripe_register_customer_payment(
            self.amount,
            journal,
            _("Cash payment - %s", order.name),
            payment_method_line=method_line,
        )
        if not order._stripe_terminal_can_view_account_payments():
            return {
                "type": "ir.actions.act_window",
                "name": _("Sales Order"),
                "res_model": "sale.order",
                "res_id": order.id,
                "view_mode": "form",
                "target": "current",
            }
        return {
            "type": "ir.actions.act_window",
            "name": _("Payment"),
            "res_model": "account.payment",
            "res_id": payments[:1].id,
            "view_mode": "form",
            "target": "current",
        }
