from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StripeTerminalRefundWizard(models.TransientModel):
    """Full or partial refund of a Stripe Terminal payment.

    The user picks a base amount up to the refundable balance. The proportional
    customer surcharge is calculated automatically and refunded in the same
    Stripe request.
    """

    _name = "stripe.terminal.refund.wizard"
    _description = "Stripe Terminal Refund Wizard"

    payment_id = fields.Many2one(
        comodel_name="stripe.terminal.payment",
        string="Payment",
        required=True,
        readonly=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    payment_amount = fields.Monetary(
        string="Payment Amount", related="payment_id.amount")
    refunded_amount = fields.Monetary(
        string="Already Refunded", related="payment_id.refunded_amount")
    amount_available_for_refund = fields.Monetary(
        string="Maximum Refund Allowed",
        compute="_compute_amount_available_for_refund",
    )
    amount_to_refund = fields.Monetary(
        string="Refund Amount",
        compute="_compute_amount_to_refund",
        store=True,
        readonly=False,
    )
    surcharge_to_refund = fields.Monetary(
        string="Surcharge to Refund",
        compute="_compute_surcharge_to_refund",
    )
    total_to_refund = fields.Monetary(
        string="Total Refund",
        compute="_compute_surcharge_to_refund",
    )
    currency_id = fields.Many2one(
        string="Currency", related="payment_id.currency_id")

    @api.depends(
        "payment_amount",
        "refunded_amount",
        "payment_id.refund_ids.status",
        "payment_id.refund_ids.base_amount",
    )
    def _compute_amount_available_for_refund(self):
        for wizard in self:
            wizard.amount_available_for_refund = (
                wizard.payment_id._get_refundable_base_amount()
                if wizard.payment_id
                else 0.0
            )

    @api.depends("amount_available_for_refund")
    def _compute_amount_to_refund(self):
        """Default the refund amount to the full refundable balance."""
        for wizard in self:
            wizard.amount_to_refund = wizard.amount_available_for_refund

    @api.depends(
        "amount_to_refund",
        "payment_id.refunded_surcharge_amount",
        "payment_id.refund_ids.status",
        "payment_id.refund_ids.surcharge_amount",
    )
    def _compute_surcharge_to_refund(self):
        for wizard in self:
            payment = wizard.payment_id
            quote = (
                payment._get_terminal_refund_quote(
                    wizard.amount_to_refund
                )
                if payment and wizard.amount_to_refund
                else {}
            )
            wizard.surcharge_to_refund = quote.get(
                "surcharge_amount",
                0.0,
            )
            wizard.total_to_refund = quote.get("gross_amount", 0.0)

    @api.constrains("amount_to_refund")
    def _check_amount_to_refund_within_boundaries(self):
        for wizard in self:
            currency = wizard.currency_id
            too_big = currency.compare_amounts(
                wizard.amount_to_refund, wizard.amount_available_for_refund) > 0
            if currency.compare_amounts(wizard.amount_to_refund, 0) <= 0 or too_big:
                raise ValidationError(_(
                    "The amount to refund must be positive and cannot be greater "
                    "than %(amount)s %(currency)s.",
                    amount=wizard.amount_available_for_refund,
                    currency=currency.name))

    def action_refund(self):
        self.ensure_one()
        refund = self.payment_id._process_refund(self.amount_to_refund)
        notification_type = (
            "success"
            if refund.status == "succeeded"
            else "danger"
            if refund.status in ("failed", "canceled", "manual_review")
            else "warning"
        )
        message = (
            _("The refund was completed and recorded in Accounting.")
            if refund.status == "succeeded"
            else _(
                "The refund is %(status)s. Accounting will be posted only "
                "after Stripe confirms success.",
                status=refund.status.replace("_", " "),
            )
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stripe Refund"),
                "message": message,
                "type": notification_type,
                "sticky": refund.status != "succeeded",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
