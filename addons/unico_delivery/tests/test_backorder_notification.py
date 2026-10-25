from odoo.tests import Form, TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnicoBackorderNotification(TransactionCase):
    """A backorder on a customer delivery emails the sale order's salesperson
    (and never the customer)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({
            "name": "Backorder Customer", "email": "bo_customer@example.com",
        })
        cls.salesperson = cls.env["res.users"].create({
            "name": "Backorder Rep", "login": "unico_bo_rep",
            "email": "unico_bo_rep@example.com",
            "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman").id])],
        })
        cls.product = cls.env["product.template"].create({
            "name": "Backorder Item", "type": "consu",
        }).product_variant_id

    def _confirmed_delivery(self, with_salesperson=True):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "user_id": self.salesperson.id if with_salesperson else False,
        })
        self.env["sale.order.line"].create({
            "order_id": order.id, "product_id": self.product.id, "product_uom_qty": 5,
        })
        order.action_confirm()
        return order, order.picking_ids[:1]

    def test_email_targets_salesperson_not_customer(self):
        order, picking = self._confirmed_delivery()
        mail_values, note_body, subject = picking._unico_backorder_notification()

        self.assertTrue(mail_values, "An alert should be built for a sale delivery.")
        self.assertIn("unico_bo_rep@example.com", mail_values["email_to"])
        self.assertNotIn("bo_customer@example.com", mail_values["email_to"])
        self.assertIn(self.product.display_name, mail_values["body_html"])

    def test_no_alert_without_salesperson(self):
        order, picking = self._confirmed_delivery(with_salesperson=False)
        self.assertEqual(
            picking._unico_backorder_notification(), (None, None, None)
        )

    def test_no_alert_without_salesperson_email(self):
        order, picking = self._confirmed_delivery()
        self.salesperson.email = False
        self.assertEqual(
            picking._unico_backorder_notification(), (None, None, None)
        )

    def test_create_backorder_sends_email_and_notifies_nobody_via_note(self):
        """Drive the real partial-delivery -> backorder-wizard flow."""
        order, picking = self._confirmed_delivery()
        picking.action_assign()
        picking.move_ids.quantity = 2
        picking.move_ids.picked = True
        before = order.message_ids

        action = picking.button_validate()
        Form.from_action(self.env, action).save().process()

        backorder = self.env["stock.picking"].search([("backorder_id", "=", picking.id)], limit=1)
        self.assertTrue(backorder, "A backorder should have been created.")

        note = (order.message_ids - before).filtered(
            lambda m: "short-shipped" in (m.body or "")
        )
        self.assertTrue(note, "An audit note should be posted on the order.")
        # The note itself notifies nobody (the email is the alert); crucially the
        # customer is never a recipient.
        self.assertNotIn(self.customer, note.partner_ids)
