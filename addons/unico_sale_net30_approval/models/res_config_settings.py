from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    unico_auto_send_net30_invoices = fields.Boolean(
        related="company_id.unico_auto_send_net30_invoices",
        string="Auto-send Net 30 Invoices",
        readonly=False,
    )
