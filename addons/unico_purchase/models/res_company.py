from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    unico_default_purchase_vendor_id = fields.Many2one(
        comodel_name="res.partner",
        string="Default Purchase Vendor",
        help="Fallback vendor used when auto-creating an RFQ for a special-order "
             "product (no Min/Max rule, not in stock) that has no vendor set on "
             "the product itself.",
    )
    unico_default_purchase_user_id = fields.Many2one(
        comodel_name="res.users",
        string="Default Purchase Representative",
        help="User set as Purchase Representative on new RFQs and Purchase Orders, "
             "instead of the user who created them. Applies to both manually and "
             "automatically created purchases; leave empty to keep Odoo's default "
             "(the creating user).",
    )
    unico_group_rfq_per_source = fields.Boolean(
        string="Group RFQs per source",
        default=True,
        help="When enabled, auto-generated reordering RFQs are grouped strictly "
             "per source: one draft PO per reordering rule (OP), and one per sale "
             "order for MTO replenishment, instead of Odoo's native vendor-level "
             "grouping. Buyers can still combine POs manually with Merge POs.",
    )
