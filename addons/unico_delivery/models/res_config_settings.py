from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    unico_send_automatic_customer_emails = fields.Boolean(
        related="company_id.unico_send_automatic_customer_emails",
        string="Automatic Customer Emails",
        readonly=False,
    )
