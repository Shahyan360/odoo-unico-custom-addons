from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    unico_send_automatic_customer_emails = fields.Boolean(
        string="Automatic Customer Emails",
        default=False,
        help="When disabled, Odoo stops mailing customers by itself: no order "
             "confirmation or payment-received mail from an online payment, no "
             "invoice mailed by the automatic-invoice flow, and no delivery "
             "confirmation when a transfer is validated. Sending by hand is "
             "unaffected — the Send by Email buttons, the Send & Print wizard "
             "and chatter messages all still go out, as do internal "
             "notifications to your own staff.",
    )
