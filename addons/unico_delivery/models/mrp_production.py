from odoo import fields, models


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    fulfillment_type = fields.Selection(
        related="sale_line_id.order_id.fulfillment_type",
        string="Fulfillment Type",
        store=True,
        index=True,
    )
