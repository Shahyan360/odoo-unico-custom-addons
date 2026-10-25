import logging

from odoo import http
from odoo.http import request

from ..models import stripe_client

_logger = logging.getLogger(__name__)


class StripeTerminalWebhookController(http.Controller):

    @http.route(
        "/unico_stripe_terminal/webhook",
        type="http",
        auth="public",
        csrf=False,
        methods=["POST"],
        save_session=False,
    )
    def stripe_terminal_webhook(self, **kwargs):
        """Receive Stripe Terminal events and update the matching payment."""
        payload = request.httprequest.get_data(as_text=True)
        sig_header = request.httprequest.headers.get("Stripe-Signature")

        if stripe_client.stripe is None:
            _logger.error("Stripe webhook received but `stripe` lib is missing.")
            return request.make_response("stripe library missing", status=500)

        event = self._verify_event(payload, sig_header)
        if event is None:
            return request.make_response("invalid signature", status=400)

        try:
            self._handle_event(event)
        except Exception:  # pragma: no cover - Stripe must retry transient failures
            _logger.exception("Error handling Stripe webhook event")
            return request.make_response("processing failed", status=500)
        return request.make_response("ok", status=200)

    def _verify_event(self, payload, sig_header):
        """Validate the signature against every Stripe provider's webhook secret."""
        stripe = stripe_client.stripe
        providers = request.env["payment.provider"].sudo().search([
            ("code", "=", "stripe"),
            ("stripe_webhook_secret", "!=", False)])
        for provider in providers:
            try:
                return stripe.Webhook.construct_event(
                    payload, sig_header, provider.stripe_webhook_secret)
            except Exception:
                continue
        _logger.info("Stripe webhook signature did not match any provider secret.")
        return None

    def _handle_event(self, event):
        sget = stripe_client.sget
        event_type = sget(event, "type")
        obj = sget(sget(event, "data") or {}, "object") or {}
        Payment = request.env["stripe.terminal.payment"].sudo()
        Refund = request.env["stripe.terminal.refund"].sudo()

        if event_type in ("payment_intent.succeeded",
                          "payment_intent.payment_failed",
                          "payment_intent.canceled"):
            intent_id = sget(obj, "id")
            payment = Payment.search([("payment_intent_id", "=", intent_id)], limit=1)
            if payment:
                payment._apply_intent_status(obj)
        elif event_type == "terminal.reader.action_succeeded":
            action = sget(obj, "action") or {}
            action_details = (
                sget(action, "collect_payment_method")
                or sget(action, "confirm_payment_intent")
                or sget(action, "process_payment_intent")
                or {}
            )
            intent_id = sget(action_details, "payment_intent")
            payment = Payment.search(
                [("payment_intent_id", "=", intent_id)],
                limit=1,
            )
            if payment:
                payment.action_check_status()
        elif event_type in (
            "refund.created",
            "refund.updated",
            "refund.failed",
        ):
            refund_id = sget(obj, "id")
            refund = Refund.search(
                [("stripe_refund_id", "=", refund_id)],
                limit=1,
            )
            if refund:
                refund._apply_stripe_status(obj)
            else:
                intent_id = sget(obj, "payment_intent")
                payment = Payment.search(
                    [("payment_intent_id", "=", intent_id)],
                    limit=1,
                )
                if payment:
                    payment.message_post(body=request.env._(
                        "Stripe reported refund %(refund)s, but it has no "
                        "matching local refund request. Accounting review is "
                        "required.",
                        refund=refund_id,
                    ))
        elif event_type == "charge.refunded":
            intent_id = sget(obj, "payment_intent")
            payment = Payment.search([("payment_intent_id", "=", intent_id)], limit=1)
            if payment:
                gross_refunded = stripe_client.from_stripe_amount(
                    sget(obj, "amount_refunded") or 0, payment.currency_id)
                locally_booked = payment.currency_id.round(
                    payment.refunded_amount
                    + payment.refunded_surcharge_amount
                )
                if payment.currency_id.compare_amounts(
                    gross_refunded,
                    locally_booked,
                ) > 0:
                    payment.message_post(body=request.env._(
                        "Stripe reports %(amount)s refunded, which exceeds the "
                        "%(booked)s currently booked locally. Accounting review "
                        "is required.",
                        amount=payment.currency_id.format(gross_refunded),
                        booked=payment.currency_id.format(locally_booked),
                    ))
                if payment.currency_id.compare_amounts(
                    gross_refunded,
                    payment.total_charged,
                ) >= 0:
                    payment.status = "refunded"
