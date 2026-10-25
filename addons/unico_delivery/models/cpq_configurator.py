from odoo import models


class CpqSpecialOrderWizard(models.TransientModel):
    """Auto-create the special-order RFQ as soon as the configure product's
    special configuration is confirmed (this is when the line first gets its
    components + vendor, so it is the correct trigger point - the order may not
    be re-saved afterwards)."""

    _inherit = "cpq.special.order.wizard"

    def action_confirm(self):
        result = super().action_confirm()
        for wizard in self:
            if wizard.order_line_id:
                wizard.order_line_id._cpq_refresh_special_rfqs()
        return result


class CpqConfiguratorWizard(models.TransientModel):
    """Also trigger on the standard configurator confirm. It is a no-op unless
    the line already carries ready special-order components (the readiness check
    guards it), so it simply covers configure products driven from this wizard."""

    _inherit = "cpq.configurator.wizard"

    def action_confirm(self):
        result = super().action_confirm()
        for wizard in self:
            if wizard.order_line_id:
                wizard.order_line_id._cpq_refresh_special_rfqs()
        return result
