# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.fields import Command


class ProjectTask(models.Model):
    _inherit = 'project.task'

    unico_is_local_delivery_task = fields.Boolean(
        string='Local Delivery Task',
        copy=False,
        index=True,
    )
    unico_sale_order_id = fields.Many2one(
        'sale.order',
        string='Delivery Sales Order',
        copy=False,
        index=True,
        ondelete='set null',
    )
    unico_delivery_picking_ids = fields.Many2many(
        'stock.picking',
        'unico_delivery_task_picking_rel',
        'task_id',
        'picking_id',
        string='Delivery Orders',
        copy=False,
    )
    unico_primary_delivery_picking_id = fields.Many2one(
        'stock.picking',
        string='Primary Delivery Order',
        copy=False,
        ondelete='set null',
    )
    unico_mrp_production_ids = fields.Many2many(
        'mrp.production',
        'unico_delivery_task_mrp_production_rel',
        'task_id',
        'production_id',
        string='Manufacturing Orders',
        copy=False,
    )
    unico_customer_id = fields.Many2one(
        'res.partner',
        string='Delivery Customer',
        copy=False,
        ondelete='set null',
    )
    unico_delivery_partner_id = fields.Many2one(
        'res.partner',
        string='Delivery Address',
        copy=False,
        ondelete='set null',
    )
    unico_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        copy=False,
        ondelete='set null',
    )
    unico_promised_delivery_datetime = fields.Datetime(
        string='Promised Delivery Date',
        copy=False,
    )
    unico_effective_delivery_datetime = fields.Datetime(
        string='Effective Delivery Date',
        copy=False,
    )
    unico_incoterm_id = fields.Many2one(
        'account.incoterms',
        string='Incoterm',
        copy=False,
        ondelete='set null',
    )
    unico_incoterm_location = fields.Char(
        string='Incoterm Location',
        copy=False,
    )
    unico_shipping_policy = fields.Selection(
        selection='_selection_unico_shipping_policy',
        string='Shipping Policy',
        copy=False,
    )
    unico_delivery_status = fields.Selection(
        selection='_selection_unico_delivery_status',
        string='Delivery Status',
        copy=False,
    )
    unico_product_summary = fields.Text(
        string='Delivery Product Summary',
        copy=False,
    )
    unico_delivery_item_count = fields.Float(
        string='Delivery Item Count',
        copy=False,
    )
    unico_delivery_instructions = fields.Text(
        string='Delivery Instructions',
        copy=False,
    )
    unico_driver_id = fields.Many2one(
        'res.users',
        string='Driver',
        copy=False,
        domain="[('share', '=', False), ('active', '=', True)]",
        ondelete='set null',
    )
    unico_vehicle_id = fields.Many2one(
        'fleet.vehicle',
        string='Vehicle',
        copy=False,
        ondelete='set null',
    )
    unico_photos_required = fields.Boolean(
        string='Photos Required',
        default=True,
        copy=False,
    )
    unico_signature_required = fields.Boolean(
        string='Signature Required',
        default=True,
        copy=False,
    )
    unico_actual_delivery_datetime = fields.Datetime(
        string='Actual Delivery Time',
        copy=False,
    )
    unico_issue_type = fields.Selection([
        ('damaged', 'Damaged Item'),
        ('missing_items', 'Missing Items'),
        ('access_issue', 'Access Issue'),
        ('customer_not_available', 'Customer Not Available'),
        ('wrong_address', 'Wrong Address'),
        ('other', 'Other'),
    ], string='Issue Type', copy=False)
    unico_issue_notes = fields.Text(
        string='Issue Notes',
        copy=False,
    )
    unico_pod_attachment_ids = fields.Many2many(
        'ir.attachment',
        'unico_delivery_task_pod_attachment_rel',
        'task_id',
        'attachment_id',
        string='Proof of Delivery Photos',
        copy=False,
    )
    unico_signature = fields.Binary(
        string='Delivery Signature',
        copy=False,
        attachment=True,
    )
    unico_delivery_picking_count = fields.Integer(
        string='Delivery Order Count',
        compute='_compute_unico_delivery_counts',
    )
    unico_mrp_production_count = fields.Integer(
        string='Manufacturing Order Count',
        compute='_compute_unico_delivery_counts',
    )

    def _compute_unico_delivery_counts(self):
        for task in self:
            task.unico_delivery_picking_count = len(task.unico_delivery_picking_ids)
            task.unico_mrp_production_count = len(task.unico_mrp_production_ids)

    def _selection_unico_shipping_policy(self):
        return self.env['sale.order']._fields['picking_policy']._description_selection(self.env)

    def _selection_unico_delivery_status(self):
        return self.env['sale.order']._fields['delivery_status']._description_selection(self.env)

    def _unico_sync_driver_to_assignees(self):
        for task in self.filtered('unico_driver_id'):
            if task.unico_driver_id not in task.user_ids:
                task.user_ids = [Command.link(task.unico_driver_id.id)]

    def write(self, vals):
        res = super().write(vals)
        if vals.get('unico_driver_id'):
            self._unico_sync_driver_to_assignees()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        tasks = super().create(vals_list)
        tasks._unico_sync_driver_to_assignees()
        return tasks

    def action_unico_view_sale_order(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('sale.action_orders')
        action.update({
            'views': [(False, 'form')],
            'res_id': self.unico_sale_order_id.id,
            'domain': [('id', '=', self.unico_sale_order_id.id)],
        })
        return action

    def action_unico_view_delivery_pickings(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('stock.action_picking_tree_all')
        pickings = self.unico_delivery_picking_ids
        action.update({
            'name': 'Delivery Orders',
            'domain': [('id', 'in', pickings.ids)],
            'context': {'contact_display': 'partner_address'},
        })
        if len(pickings) == 1:
            action.update({'views': [(False, 'form')], 'res_id': pickings.id})
        return action

    def action_unico_view_mrp_productions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mrp.mrp_production_action')
        productions = self.unico_mrp_production_ids
        action['domain'] = [('id', 'in', productions.ids)]
        if len(productions) == 1:
            action.update({'views': [(False, 'form')], 'res_id': productions.id})
        return action

    def action_unico_mark_delivered(self):
        delivered_stage = self.env.ref('unico_delivery_fsm.stage_delivered', raise_if_not_found=False)
        vals = {
            'unico_actual_delivery_datetime': fields.Datetime.now(),
        }
        done_state = self._unico_get_available_task_state('1_done')
        if done_state:
            vals['state'] = done_state
        if delivered_stage:
            vals['stage_id'] = delivered_stage.id
        self.write(vals)
        return True

    def action_unico_mark_delivery_issue(self):
        issue_stage = self.env.ref('unico_delivery_fsm.stage_delivery_issue', raise_if_not_found=False)
        vals = {}
        changes_requested_state = self._unico_get_available_task_state('02_changes_requested')
        if changes_requested_state:
            vals['state'] = changes_requested_state
        if issue_stage:
            vals['stage_id'] = issue_stage.id
        if vals:
            self.write(vals)
        return True

    def action_unico_cancel_delivery_request(self):
        cancelled_stage = self.env.ref('unico_delivery_fsm.stage_cancelled', raise_if_not_found=False)
        vals = {}
        cancelled_state = self._unico_get_available_task_state('1_canceled')
        if cancelled_state:
            vals['state'] = cancelled_state
        if cancelled_stage:
            vals['stage_id'] = cancelled_stage.id
        if vals:
            self.write(vals)
        return True

    def action_unico_reopen_delivery_request(self):
        waiting_stage = self.env.ref('unico_delivery_fsm.stage_waiting_for_production', raise_if_not_found=False)
        vals = {}
        in_progress_state = self._unico_get_available_task_state('01_in_progress')
        if in_progress_state:
            vals['state'] = in_progress_state
        if waiting_stage:
            vals['stage_id'] = waiting_stage.id
        if vals:
            self.write(vals)
        return True

    def _unico_is_cancelled_delivery_request(self):
        cancelled_stage = self.env.ref('unico_delivery_fsm.stage_cancelled', raise_if_not_found=False)
        cancelled_state = self._unico_get_available_task_state('1_canceled')
        self.ensure_one()
        return (
            (cancelled_stage and self.stage_id == cancelled_stage)
            or (cancelled_state and self.state == cancelled_state)
        )

    def _unico_get_available_task_state(self, state):
        state_field = self._fields.get('state')
        if state_field and state in state_field.get_values(self.env):
            return state
        return False
