from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    unico_default_purchase_vendor_id = fields.Many2one(
        related="company_id.unico_default_purchase_vendor_id",
        string="Default Purchase Vendor",
        readonly=False,
    )
    unico_default_purchase_user_id = fields.Many2one(
        related="company_id.unico_default_purchase_user_id",
        string="Default Purchase Representative",
        readonly=False,
    )
    unico_group_rfq_per_source = fields.Boolean(
        related="company_id.unico_group_rfq_per_source",
        string="Group RFQs per source",
        readonly=False,
    )
