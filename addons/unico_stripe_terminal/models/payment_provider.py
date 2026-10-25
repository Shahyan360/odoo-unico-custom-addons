import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import stripe_client

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    stripe_terminal_default_location_id = fields.Many2one(
        comodel_name="stripe.terminal.location",
        string="Default Terminal Location",
        domain="[('company_id', '=', company_id)]",
        help="Pre-selected location when collecting a terminal payment.",
    )
    ach_pre_msg = fields.Html(
        string="ACH Help Message",
        translate=True,
        help="Message displayed to the customer under the ACH (bank debit) "
             "payment method on the payment page. Works like the standard "
             "Help Message but is shown only for ACH.",
    )
    unico_cash_journal_id = fields.Many2one(
        comodel_name="account.journal",
        string="Cash Journal",
        domain="[('type', 'in', ('bank', 'cash', 'credit')),"
               " ('company_id', '=', company_id)]",
        help="Journal that records cash collected through the Collect Payment "
             "wizard on a Sales Order. Leave empty to deposit cash into the "
             "provider's Payment Journal, alongside card takings.",
    )
    unico_cash_payment_method_line_id = fields.Many2one(
        comodel_name="account.payment.method.line",
        string="Cash Payment Method",
        domain="[('journal_id', '=', unico_cash_journal_id),"
               " ('payment_type', '=', 'inbound')]",
        help="Payment method recorded on cash payments, so they can be told "
             "apart from card payments sharing the same journal. Only the "
             "inbound methods of the Cash Journal above are selectable; leave "
             "empty to let Odoo pick that journal's default.",
    )

    @api.onchange("unico_cash_journal_id")
    def _onchange_unico_cash_journal_id(self):
        """A payment method line belongs to one journal, so a line picked for
        the previous journal is never valid for the new one."""
        for provider in self:
            line = provider.unico_cash_payment_method_line_id
            if line and line.journal_id != provider.unico_cash_journal_id:
                provider.unico_cash_payment_method_line_id = False

    @api.constrains("unico_cash_journal_id", "unico_cash_payment_method_line_id")
    def _check_unico_cash_payment_method_line_id(self):
        for provider in self:
            line = provider.unico_cash_payment_method_line_id
            if not line:
                continue
            if not provider.unico_cash_journal_id:
                raise ValidationError(_(
                    "Choose a Cash Journal before setting a Cash Payment Method "
                    "on provider %s.", provider.display_name))
            if line.journal_id != provider.unico_cash_journal_id:
                raise ValidationError(_(
                    "Cash Payment Method %(method)s does not belong to Cash "
                    "Journal %(journal)s.",
                    method=line.display_name,
                    journal=provider.unico_cash_journal_id.display_name))

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    @api.model
    def _find_terminal_provider(self, company=None):
        """Return the active Stripe provider for ``company``, or an empty
        recordset. Use for work that can still proceed without Stripe (cash
        collection); ``_get_terminal_provider`` for work that cannot."""
        company = company or self.env.company
        return self.sudo().search([
            ("code", "=", "stripe"),
            ("company_id", "=", company.id),
            ("state", "in", ("enabled", "test")),
        ], limit=1)

    @api.model
    def _get_terminal_provider(self, company=None):
        """Return the active Stripe provider for ``company`` (or env company)."""
        company = company or self.env.company
        provider = self._find_terminal_provider(company)
        if not provider:
            raise UserError(_(
                "No active Stripe payment provider found for company %s. "
                "Enable Stripe under Sales > Configuration > Payment Providers "
                "and enter your API keys.", company.display_name))
        return provider

    @api.model
    def _get_terminal_settlement_journal(self, company=None):
        """Journal that receives card money collected on a terminal.

        Falls back to any bank/cash journal of the company when the provider
        carries none, so an incomplete provider setup never blocks collection.
        """
        company = company or self.env.company
        provider = self._find_terminal_provider(company)
        journal = provider.journal_id or self.env["account.journal"].sudo().search([
            ("type", "in", ("bank", "cash")),
            ("company_id", "=", company.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No payment journal configured. Set one on the Stripe payment "
                "provider (Sales > Configuration > Payment Providers)."))
        return journal

    @api.model
    def _get_cash_settlement_values(self, company=None):
        """Return ``(journal, payment_method_line)`` for a counter cash payment.

        Both come from the Stripe provider's **Cash Journal** / **Cash Payment
        Method** settings. With no Cash Journal set, cash is deposited into the
        same journal as card takings and Odoo picks that journal's default
        method — which is why the method line is only ever honoured when it
        belongs to the resolved journal.
        """
        company = company or self.env.company
        provider = self._find_terminal_provider(company)
        journal = provider.unico_cash_journal_id
        if not journal:
            return self._get_terminal_settlement_journal(company), \
                self.env["account.payment.method.line"]
        method_line = provider.unico_cash_payment_method_line_id
        if method_line.journal_id != journal:
            method_line = self.env["account.payment.method.line"]
        return journal, method_line

    def _stripe_terminal_get_lib(self):
        self.ensure_one()
        return stripe_client.get_stripe(self)

    # ------------------------------------------------------------------
    # Configuration actions (buttons on the Stripe provider form)
    # ------------------------------------------------------------------
    def action_stripe_terminal_test_connection(self):
        self.ensure_one()
        stripe = self._stripe_terminal_get_lib()
        try:
            stripe.Account.retrieve()
        except Exception as ex:
            _logger.exception("Stripe connection test failed")
            raise UserError(_("Stripe connection failed: %s", ex))
        return self._stripe_terminal_notify(_("Connection successful."))

    def action_stripe_terminal_sync_locations(self):
        self.ensure_one()
        stripe = self._stripe_terminal_get_lib()
        Location = self.env["stripe.terminal.location"]
        count = 0
        try:
            locations = stripe.terminal.Location.list(limit=100)
        except Exception as ex:
            _logger.exception("Stripe location sync failed")
            raise UserError(_("Could not list Stripe locations: %s", ex))
        for loc in locations.auto_paging_iter():
            Location._upsert_from_stripe(loc, self.company_id)
            count += 1
        return self._stripe_terminal_notify(
            _("%s location(s) synced from Stripe.", count))

    def action_stripe_terminal_sync_readers(self):
        self.ensure_one()
        stripe = self._stripe_terminal_get_lib()
        # Sync locations first so readers can be linked.
        self.action_stripe_terminal_sync_locations()
        Reader = self.env["stripe.terminal.reader"]
        count = 0
        try:
            readers = stripe.terminal.Reader.list(limit=100)
        except Exception as ex:
            _logger.exception("Stripe reader sync failed")
            raise UserError(_("Could not list Stripe readers: %s", ex))
        for reader in readers.auto_paging_iter():
            Reader._upsert_from_stripe(reader, self.company_id)
            count += 1
        return self._stripe_terminal_notify(
            _("%s reader(s) synced from Stripe.", count))

    def action_stripe_terminal_create_simulated_reader(self):
        """Register Stripe's simulated reader for hardware-free testing."""
        self.ensure_one()
        if self.state != "test":
            raise UserError(_(
                "Simulated readers can only be created while the provider is in "
                "Test Mode."))
        stripe = self._stripe_terminal_get_lib()
        location = self.stripe_terminal_default_location_id or self.env[
            "stripe.terminal.location"].search(
                [("company_id", "=", self.company_id.id)], limit=1)
        if not location:
            raise UserError(_(
                "Create or sync at least one Terminal location before "
                "registering a simulated reader."))
        try:
            reader = stripe.terminal.Reader.create(
                registration_code="simulated-wpe",
                location=location.stripe_location_id,
                label="Simulated WisePOS E",
            )
        except Exception as ex:
            _logger.exception("Simulated reader creation failed")
            raise UserError(_("Could not create simulated reader: %s", ex))
        record = self.env["stripe.terminal.reader"]._upsert_from_stripe(
            reader, self.company_id)
        record.is_simulated = True
        return {
            "type": "ir.actions.act_window",
            "res_model": "stripe.terminal.reader",
            "res_id": record.id,
            "view_mode": "form",
            "target": "current",
        }

    def _stripe_terminal_notify(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stripe Terminal"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }
