# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class MultiInvoicePaymentLink(models.TransientModel):
    _name = "unico.multi.invoice.payment.link"
    _description = "Multi-Invoice Payment Link"

    invoice_ids = fields.Many2many(
        comodel_name="account.move",
        string="Invoices",
        readonly=True,
    )
    partner_id = fields.Many2one("res.partner", string="Customer", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)
    amount = fields.Monetary(string="Amount To Pay", readonly=True)
    payment_link = fields.Char(string="Payment Link", readonly=True)
