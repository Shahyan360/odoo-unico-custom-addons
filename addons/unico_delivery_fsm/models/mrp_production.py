# -*- coding: utf-8 -*-
from odoo import api, models


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    @api.model_create_multi
    def create(self, vals_list):
        productions = super().create(vals_list)
        productions._sync_unico_delivery_tasks_from_productions()
        return productions

    def write(self, vals):
        res = super().write(vals)
        if {'sale_line_id', 'origin'} & set(vals):
            self._sync_unico_delivery_tasks_from_productions()
        return res

    def _sync_unico_delivery_tasks_from_productions(self):
        orders = self.mapped('sale_line_id.order_id').filtered('unico_local_delivery_required')
        if orders:
            orders._sync_unico_delivery_tasks()
