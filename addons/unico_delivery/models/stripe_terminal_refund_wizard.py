from odoo import models


class StripeTerminalRefundWizard(models.TransientModel):
    # Ensure the transient table is initialized when Odoo.sh upgrades only
    # unico_delivery, even though the wizard is defined by unico_stripe_terminal.
    _inherit = "stripe.terminal.refund.wizard"
