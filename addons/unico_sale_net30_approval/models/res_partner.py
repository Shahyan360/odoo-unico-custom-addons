from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    unico_po_alert = fields.Boolean(
        string="PO Alert",
        help="This customer requires their own purchase order details on every "
             "sales order. Orders for them cannot be confirmed until PO Detail "
             "is filled in.\n"
             "Set this on the company contact — every contact underneath it is "
             "covered.",
    )
