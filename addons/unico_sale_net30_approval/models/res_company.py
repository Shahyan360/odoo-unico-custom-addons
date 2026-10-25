from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    unico_auto_send_net30_invoices = fields.Boolean(
        string="Auto-send Net 30 Invoices",
        default=True,
        help="Email the invoice to the customer as soon as it is validated, "
             "whenever its payment term is a Net 30 term. The mail carries the "
             "invoice PDF and a link to view and pay it on the portal.\n"
             "Deliberately independent of the general 'Automatic Customer "
             "Emails' setting, so Net 30 billing can run on its own without "
             "also switching on order-confirmation and delivery emails.",
    )
