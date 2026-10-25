# -*- coding: utf-8 -*-
from odoo import models


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _unico_incoterm_delivery_location(self):
        """Incoterm location note for the Invoice PDF, via the source sale order(s)."""
        self.ensure_one()
        for order in self.line_ids.sale_line_ids.order_id:
            location = order._unico_incoterm_delivery_location()
            if location:
                return location
        return False
