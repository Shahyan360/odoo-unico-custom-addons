from odoo import fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    unico_auto_special_rfq = fields.Boolean(
        string="Auto Special RFQ",
        copy=False,
        help="Marks RFQs created or maintained by the Unico special-order "
             "automation.",
    )
