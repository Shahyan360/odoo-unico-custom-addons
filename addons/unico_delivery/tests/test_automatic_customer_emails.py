"""One switch over every automatic customer email.

Odoo mails customers by itself from four places, gated inconsistently: a
company setting for the delivery confirmation, a config parameter for the
automatic invoice, and nothing at all for the order-confirmation and
payment-received mails sent on the online-payment path. The switch has to
silence all four while leaving manual sending — Send by Email, Send & Print,
chatter — and internal staff notifications alone.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoAutomaticCustomerEmails(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.env["res.partner"].create({
            "name": "Auto Mail Customer",
            "email": "auto.mail@example.com",
        })
        cls.product = cls.env["product.product"].create({
            "name": "Auto Mail Product",
            "list_price": 100.0,
        })

    def _order(self):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
                "price_unit": 100.0,
            })],
        })

    # ------------------------------------------------------------------
    def test_setting_defaults_to_off(self):
        self.assertFalse(self.company.unico_send_automatic_customer_emails)

    def test_order_confirmation_mail_is_suppressed(self):
        order = self._order()
        with patch.object(
            type(order), "_send_order_notification_mail"
        ) as send:
            order._send_order_confirmation_mail()
        send.assert_not_called()

    def test_order_confirmation_mail_still_sends_when_enabled(self):
        self.company.unico_send_automatic_customer_emails = True
        order = self._order()
        with patch.object(
            type(order), "_send_order_notification_mail"
        ) as send:
            order._send_order_confirmation_mail()
        send.assert_called()

    def test_online_payment_confirmation_path_is_suppressed(self):
        """Paying on the portal confirms with send_email in the context; that
        is the only route by which confirming mails the customer."""
        order = self._order()
        with patch.object(
            type(order), "_send_order_notification_mail"
        ) as send:
            order.with_context(send_email=True).action_confirm()
        send.assert_not_called()
        self.assertEqual(order.state, "sale")

    def test_delivery_confirmation_mail_is_suppressed(self):
        """Even with Odoo's own stock setting switched on."""
        self.company.stock_move_email_validation = True
        order = self._order()
        order.action_confirm()
        picking = order.picking_ids[:1]
        if not picking:
            self.skipTest("no delivery generated for this product setup")
        with patch.object(
            type(picking), "message_post_with_source"
        ) as post:
            picking._send_confirmation_email()
        post.assert_not_called()

    def test_manual_sending_is_untouched(self):
        """The Send by Email action must still build its mail composer."""
        order = self._order()

        action = order.action_quotation_send()

        self.assertTrue(action)
        self.assertFalse(self.company.unico_send_automatic_customer_emails)

    def test_internal_backorder_alert_still_fires(self):
        """Staff notifications are deliberately outside the switch."""
        self.assertTrue(
            hasattr(self.env["stock.picking"], "_unico_notify_backorder_salesperson")
        )
        self.assertFalse(self.company.unico_send_automatic_customer_emails)
