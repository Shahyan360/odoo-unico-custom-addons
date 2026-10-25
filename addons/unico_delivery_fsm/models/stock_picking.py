# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .sale_order import SKIP_DELIVERY_TASK_SYNC_CONTEXT


SKIP_DELIVERY_CANCEL_SYNC_CONTEXT = 'unico_skip_delivery_cancel_sync'


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    unico_delivery_task_id = fields.Many2one(
        'project.task',
        string='Delivery Task',
        compute='_compute_unico_delivery_task_id',
    )

    @api.depends('sale_id.unico_delivery_task_id')
    def _compute_unico_delivery_task_id(self):
        for picking in self:
            picking.unico_delivery_task_id = picking.sale_id.unico_delivery_task_id

    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        pickings._sync_unico_delivery_tasks_from_pickings()
        return pickings

    def write(self, vals):
        res = super().write(vals)
        sync_fields = {'sale_id', 'scheduled_date', 'picking_type_id', 'location_dest_id'}
        # Normal cancellation is handled by action_cancel(); this branch only covers direct writes/imports.
        direct_cancel = vals.get('state') == 'cancel' and not self.env.context.get(SKIP_DELIVERY_CANCEL_SYNC_CONTEXT)
        if sync_fields & set(vals) or direct_cancel:
            self._sync_unico_delivery_tasks_from_pickings()
        return res

    def action_cancel(self):
        res = super(StockPicking, self.with_context(**{SKIP_DELIVERY_CANCEL_SYNC_CONTEXT: True})).action_cancel()
        self._sync_unico_delivery_tasks_from_pickings()
        return res

    def _action_done(self):
        res = super(StockPicking, self.with_context(**{SKIP_DELIVERY_TASK_SYNC_CONTEXT: True}))._action_done()
        self._sync_unico_delivery_tasks_from_pickings()
        return res

    def _sync_unico_delivery_tasks_from_pickings(self):
        orders = self.mapped('sale_id').filtered('unico_local_delivery_required')
        if orders:
            orders._sync_unico_delivery_tasks()

    def action_view_unico_delivery_task(self):
        self.ensure_one()
        return self.sale_id.action_view_unico_delivery_task()

    def _unico_incoterm_delivery_location(self):
        """Incoterm location note for the Delivery Slip PDF, via the linked sale order."""
        self.ensure_one()
        if self.sale_id:
            return self.sale_id._unico_incoterm_delivery_location()
        return False
