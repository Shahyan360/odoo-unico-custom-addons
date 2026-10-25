from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.unico_stripe_terminal.models import stripe_client

from .common import StripeTerminalCommon


@tagged("post_install", "-at_install")
class TestStripeTerminalHelpers(StripeTerminalCommon):

    def test_to_stripe_amount_decimal_currency(self):
        usd = self.env.ref("base.USD")
        self.assertEqual(stripe_client.to_stripe_amount(100.0, usd), 10000)
        self.assertEqual(stripe_client.to_stripe_amount(12.34, usd), 1234)

    def test_to_stripe_amount_zero_decimal_currency(self):
        jpy = self.env.ref("base.JPY")
        # Zero-decimal currencies must be sent as whole units.
        self.assertEqual(stripe_client.to_stripe_amount(1500.0, jpy), 1500)

    def test_round_trip_amount(self):
        usd = self.env.ref("base.USD")
        minor = stripe_client.to_stripe_amount(99.99, usd)
        self.assertEqual(minor, 9999)
        self.assertAlmostEqual(stripe_client.from_stripe_amount(minor, usd), 99.99, 2)


@tagged("post_install", "-at_install")
class TestStripeTerminalUpsert(StripeTerminalCommon):

    def test_location_upsert_is_idempotent(self):
        Location = self.env["stripe.terminal.location"]
        payload = {
            "id": "tml_sync",
            "display_name": "Synced Store",
            "address": {"city": "Berlin", "country": "DE"},
        }
        first = Location._upsert_from_stripe(payload, self.company)
        self.assertEqual(first.name, "Synced Store")
        self.assertEqual(first.address_city, "Berlin")
        # A second sync of the same Stripe id must update, not duplicate.
        payload["display_name"] = "Renamed Store"
        second = Location._upsert_from_stripe(payload, self.company)
        self.assertEqual(first, second)
        self.assertEqual(second.name, "Renamed Store")
        self.assertEqual(Location.search_count([("stripe_location_id", "=", "tml_sync")]), 1)

    def test_reader_upsert_links_location(self):
        Reader = self.env["stripe.terminal.reader"]
        payload = {
            "id": "tmr_sync",
            "label": "Synced Reader",
            "device_type": "stripe_s700",
            "status": "online",
            "location": "tml_main",
            "serial_number": "SN-123",
        }
        reader = Reader._upsert_from_stripe(payload, self.company)
        self.assertEqual(reader.location_id, self.location)
        self.assertEqual(reader.status, "online")
        self.assertEqual(reader.device_type, "stripe_s700")

    def test_reader_display_name_marks_offline(self):
        self.reader.status = "offline"
        self.assertIn("offline", self.reader.display_name)
        self.reader.status = "online"
        self.assertNotIn("offline", self.reader.display_name)


@tagged("post_install", "-at_install")
class TestStripeTerminalSaleOrder(StripeTerminalCommon):

    def _make_salesperson_without_accounting(self):
        user = new_test_user(
            self.env,
            login="terminal_sales_no_account",
            groups="base.group_user,sales_team.group_sale_salesman",
            company_id=self.company.id,
            company_ids=[(6, 0, self.company.ids)],
        )
        self.sale_order.user_id = user.id
        self.assertTrue(
            user.has_group("unico_stripe_terminal.group_stripe_terminal_user"))
        self.assertFalse(user.has_groups(
            "account.group_account_invoice,account.group_account_readonly"))
        return user

    def test_payment_count_compute(self):
        self.assertEqual(self.sale_order.stripe_terminal_payment_count, 0)
        self._collect_cash(40.0)
        self.sale_order.invalidate_recordset(["stripe_terminal_payment_count"])
        self.assertEqual(self.sale_order.stripe_terminal_payment_count, 1)

    def test_amount_due_and_collected(self):
        order = self.sale_order
        # No invoice needed: due defaults to the order total.
        self.assertAlmostEqual(
            order.stripe_terminal_amount_due, order.amount_total, 2)
        self.assertEqual(order.stripe_terminal_amount_collected, 0.0)
        # A collected payment reduces the due and raises the collected total.
        self._collect_cash(40.0)
        order.invalidate_recordset([
            "stripe_terminal_amount_due", "stripe_terminal_amount_collected"])
        self.assertAlmostEqual(order.stripe_terminal_amount_collected, 40.0, 2)
        self.assertAlmostEqual(
            order.stripe_terminal_amount_due, order.amount_total - 40.0, 2)

    def test_collect_when_nothing_due_raises(self):
        order = self.sale_order
        self._collect_cash(order.amount_total)
        order.invalidate_recordset(["stripe_terminal_amount_due"])
        with self.assertRaises(UserError):
            order.action_collect_stripe_terminal_payment()

    def test_collect_action_opens_wizard_prefilled(self):
        # No invoice required; default amount is the order's amount due.
        action = self.sale_order.action_collect_stripe_terminal_payment()
        self.assertEqual(action["res_model"], "stripe.terminal.payment.wizard")
        ctx = action["context"]
        self.assertEqual(ctx["default_sale_order_id"], self.sale_order.id)
        self.assertEqual(ctx["default_partner_id"], self.partner.id)
        self.assertAlmostEqual(
            ctx["default_amount"], self.sale_order.amount_total, 2)

    def test_salesperson_without_accounting_can_read_payment_summary(self):
        self._collect_cash(40.0)
        sales_user = self._make_salesperson_without_accounting()
        payment = self.env["account.payment"].sudo().search([
            ("sale_order_id", "=", self.sale_order.id)], limit=1)

        with self.assertRaises(AccessError):
            payment.with_user(sales_user).read(["amount"])

        order = self.sale_order.with_user(sales_user)
        fields_to_read = [
            "stripe_terminal_amount_collected",
            "stripe_terminal_amount_due",
            "stripe_terminal_payment_count",
        ]
        if "unico_payment_status" in order._fields:
            fields_to_read.append("unico_payment_status")
        values = order.read(fields_to_read)[0]

        self.assertEqual(values["stripe_terminal_payment_count"], 1)
        self.assertAlmostEqual(values["stripe_terminal_amount_collected"], 40.0, 2)
        self.assertAlmostEqual(
            values["stripe_terminal_amount_due"],
            self.sale_order.amount_total - 40.0,
            2,
        )
        if "unico_payment_status" in values:
            # A collected payment exists (40 of 100), so the order reads as Paid
            # even though it is only partially covered.
            self.assertEqual(values["unico_payment_status"], "paid")

        action = order.action_view_stripe_terminal_payments()
        self.assertEqual(action["res_model"], "stripe.terminal.payment")

    def test_salesperson_without_accounting_can_collect_cash(self):
        sales_user = self._make_salesperson_without_accounting()
        order = self.sale_order.with_user(sales_user)

        collect_action = order.action_collect_stripe_terminal_payment()
        self.assertEqual(
            collect_action["res_model"], "stripe.terminal.payment.wizard")

        wizard = self.env["stripe.terminal.payment.wizard"].with_user(
            sales_user
        ).create({
            "payment_method": "cash",
            "sale_order_id": self.sale_order.id,
            "partner_id": self.partner.id,
            "amount": 25.0,
            "currency_id": self.sale_order.currency_id.id,
        })
        action = wizard.action_collect()

        self.assertEqual(action["res_model"], "sale.order")
        payment = self.env["account.payment"].sudo().search([
            ("sale_order_id", "=", self.sale_order.id)], order="id desc", limit=1)
        self.assertEqual(payment.move_id.state, "posted")
        self.assertAlmostEqual(payment.amount, 25.0, 2)
        with self.assertRaises(AccessError):
            payment.with_user(sales_user).read(["amount"])

    def test_payment_sequence_assigned(self):
        payment = self._make_payment()
        self.assertTrue(payment.name.startswith("STP/"))
