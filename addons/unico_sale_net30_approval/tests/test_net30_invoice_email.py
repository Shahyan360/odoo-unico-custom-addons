"""Automatic emailing of Net 30 customer invoices on validation.

Covers which terms count as Net 30, which moves qualify, the company switch,
that the mail carries a portal link the customer can pay from, and that a
failure to send can never undo the posting.
"""
from unittest.mock import patch

from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestNet30InvoiceEmail(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.unico_auto_send_net30_invoices = True
        cls.partner_a.email = "customer@example.com"

        Term = cls.env["account.payment.term"]
        cls.net30 = Term.create({"name": "Net 30 Days"})
        cls.immediate = Term.create({"name": "Immediate Payment"})

    def _invoice(self, term=None, move_type="out_invoice", partner=None):
        return self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": (partner or self.partner_a).id,
            "invoice_date": "2026-01-01",
            "invoice_payment_term_id": term.id if term else False,
            "invoice_line_ids": [Command.create({
                "name": "Test line",
                "quantity": 1,
                "price_unit": 100.0,
            })],
        })

    def _mails_for(self, move):
        return self.env["mail.mail"].sudo().search([
            ("model", "=", "account.move"), ("res_id", "=", move.id)])

    # -- What counts as Net 30 --------------------------------------------

    def test_term_name_variants_are_recognised(self):
        Term = self.env["account.payment.term"]
        for name in ("Net 30 Days", "NET 30", "net 30", "net30", "Net-30"):
            self.assertTrue(
                Term.create({"name": name})._unico_is_net30(),
                "%r should count as Net 30" % name)

    def test_other_terms_are_not_net30(self):
        Term = self.env["account.payment.term"]
        for name in ("Immediate Payment", "Net 60 Days", "Net 300 Days", "30%"):
            self.assertFalse(
                Term.create({"name": name})._unico_is_net30(),
                "%r should not count as Net 30" % name)

    # -- Who gets mailed ---------------------------------------------------

    def test_net30_invoice_is_mailed_on_posting(self):
        invoice = self._invoice(self.net30)
        self.assertFalse(self._mails_for(invoice))
        invoice.action_post()
        self.assertTrue(self._mails_for(invoice))
        self.assertTrue(invoice.is_move_sent)

    def test_other_payment_terms_are_not_mailed(self):
        invoice = self._invoice(self.immediate)
        invoice.action_post()
        self.assertFalse(self._mails_for(invoice))
        self.assertFalse(invoice.is_move_sent)

    def test_invoice_without_a_payment_term_is_not_mailed(self):
        invoice = self._invoice(term=None)
        invoice.action_post()
        self.assertFalse(self._mails_for(invoice))

    def test_credit_note_is_not_mailed(self):
        """There is nothing to pay, so the pay-online mail does not apply."""
        refund = self._invoice(self.net30, move_type="out_refund")
        refund.action_post()
        self.assertFalse(self._mails_for(refund))

    def test_vendor_bill_is_not_mailed(self):
        bill = self._invoice(self.net30, move_type="in_invoice")
        bill.action_post()
        self.assertFalse(self._mails_for(bill))

    def test_customer_without_an_email_is_skipped(self):
        silent = self.env["res.partner"].create({"name": "No Email Co"})
        invoice = self._invoice(self.net30, partner=silent)
        invoice.action_post()
        self.assertFalse(self._mails_for(invoice))

    def test_company_switch_turns_it_off(self):
        self.company.unico_auto_send_net30_invoices = False
        invoice = self._invoice(self.net30)
        invoice.action_post()
        self.assertFalse(self._mails_for(invoice))

    def test_already_sent_invoice_is_not_mailed_twice(self):
        """Reset-to-draft and re-post must not spam the customer."""
        invoice = self._invoice(self.net30)
        invoice.action_post()
        self.assertEqual(len(self._mails_for(invoice)), 1)

        invoice.button_draft()
        invoice.action_post()
        self.assertEqual(len(self._mails_for(invoice)), 1)

    # -- What the mail contains -------------------------------------------

    def test_mail_carries_a_portal_link_to_pay(self):
        invoice = self._invoice(self.net30)
        invoice.action_post()
        body = self._mails_for(invoice).body_html or ""
        self.assertIn(invoice._portal_ensure_token(), body)
        self.assertIn("View Invoice", body)

    def test_mail_goes_to_the_customer(self):
        invoice = self._invoice(self.net30)
        invoice.action_post()
        self.assertIn(
            self.partner_a, self._mails_for(invoice).recipient_ids)

    # -- Posting is never at risk -----------------------------------------

    def test_a_failing_send_still_leaves_the_invoice_posted(self):
        """An email is not worth unwinding an accounting entry over."""
        invoice = self._invoice(self.net30)
        with patch(
            "odoo.addons.mail.models.mail_template.MailTemplate.send_mail",
            side_effect=ValueError("report blew up"),
        ):
            invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertFalse(invoice.is_move_sent)
        # The failure is left where accounting will see it.
        self.assertTrue(invoice.message_ids.filtered(
            lambda message: "could not be sent" in (message.body or "")))
