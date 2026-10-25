# -*- coding: utf-8 -*-
from collections import OrderedDict

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Command


SKIP_DELIVERY_TASK_SYNC_CONTEXT = 'unico_skip_delivery_task_sync'


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    unico_local_delivery_required = fields.Boolean(
        string='Local Delivery Required',
        copy=False,
        tracking=True,
        help=(
            'Create and maintain a Field Service delivery task for this order. '
            'Using Create/Sync Delivery Task also marks the order as a local delivery.'
        ),
    )
    unico_delivery_instructions = fields.Text(
        string='Delivery Instructions',
        copy=False,
        tracking=True,
    )
    unico_delivery_task_id = fields.Many2one(
        'project.task',
        string='Delivery Task',
        copy=False,
        readonly=True,
        ondelete='set null',
    )
    unico_delivery_task_count = fields.Integer(
        string='Delivery Task Count',
        compute='_compute_unico_delivery_task_count',
        store=True,
    )

    @api.depends('unico_delivery_task_id')
    def _compute_unico_delivery_task_count(self):
        for order in self:
            order.unico_delivery_task_count = 1 if order.unico_delivery_task_id else 0

    def _check_unico_delivery_address_required(self):
        """Require a structured shipping address on local-delivery orders.

        Enforced at confirmation (not as a create/write constraint): the shipping
        partner is used as the address on customer and operations documents.
        Checking on every save would block the
        "Sync Delivery Task" flow, which marks an order local before an incoterm
        has been entered.
        """
        for order in self:
            if order.unico_local_delivery_required and not order.partner_shipping_id:
                raise ValidationError(
                    "Shipping Address is required when Local Delivery Required is set, "
                    "as it is used as the delivery address on downstream documents."
                )

    def _unico_incoterm_delivery_location(self):
        """Incoterm location note for downstream documents.

        Printed whenever an incoterm location is captured on the order,
        independent of the local-delivery flag — the location note is relevant
        to the customer/carrier regardless of how the order is fulfilled."""
        self.ensure_one()
        return self.incoterm_location or False

    def action_confirm(self):
        self._check_unico_delivery_address_required()
        res = super().action_confirm()
        self._sync_unico_delivery_tasks()
        return res

    def write(self, vals):
        disabling_delivery_orders = self.env['sale.order']
        if vals.get('unico_local_delivery_required') is False:
            disabling_delivery_orders = self.filtered(
                lambda order: order.unico_local_delivery_required and order.unico_delivery_task_id
            )

        res = super().write(vals)

        if disabling_delivery_orders:
            disabling_delivery_orders.mapped('unico_delivery_task_id').sudo().action_unico_cancel_delivery_request()

        watched_fields = {
            'commitment_date',
            'effective_date',
            'incoterm',
            'incoterm_location',
            'expected_date',
            'partner_shipping_id',
            'picking_policy',
            'delivery_status',
            'unico_delivery_instructions',
            'unico_local_delivery_required',
            'warehouse_id',
        }
        if not self.env.context.get(SKIP_DELIVERY_TASK_SYNC_CONTEXT) and watched_fields & set(vals):
            self.filtered('unico_local_delivery_required')._sync_unico_delivery_tasks()
        return res

    def action_sync_unico_delivery_task(self):
        to_mark_local = self.filtered(lambda order: not order.unico_local_delivery_required)
        if to_mark_local:
            to_mark_local.with_context(**{SKIP_DELIVERY_TASK_SYNC_CONTEXT: True}).write({
                'unico_local_delivery_required': True,
            })
        self._sync_unico_delivery_tasks(force=True)
        return True

    def action_view_unico_delivery_task(self):
        self.ensure_one()
        task = self.unico_delivery_task_id
        action = self.env['ir.actions.act_window']._for_xml_id('project.action_view_task')
        action.update({
            'name': 'Delivery Task',
            'domain': [('id', '=', task.id)],
        })
        if task:
            action.update({'views': [(False, 'form')], 'res_id': task.id})
        return action

    def _sync_unico_delivery_tasks(self, force=False):
        for order in self:
            if not force and not order.unico_local_delivery_required:
                continue
            if order.state not in ('sale', 'done'):
                continue
            order._sync_unico_delivery_task()

    def _sync_unico_delivery_task(self):
        self.ensure_one()
        Task = self.env['project.task'].sudo()
        task = self.unico_delivery_task_id.sudo()
        if not task:
            task = Task.search([
                ('unico_sale_order_id', '=', self.id),
                ('unico_is_local_delivery_task', '=', True),
            ], limit=1)

        vals = self._prepare_unico_delivery_task_vals(create=not bool(task))
        if task:
            reopen_cancelled_task = task._unico_is_cancelled_delivery_request()
            task.write(vals)
            if reopen_cancelled_task:
                task.action_unico_reopen_delivery_request()
        else:
            task = Task.create(vals)

        if self.unico_delivery_task_id != task:
            self.sudo().write({'unico_delivery_task_id': task.id})
        return task

    def _prepare_unico_delivery_task_vals(self, create=False):
        self.ensure_one()
        project = self._get_unico_delivery_project()
        stage = self.env.ref('unico_delivery_fsm.stage_waiting_for_production', raise_if_not_found=False)
        delivery_pickings = self._get_unico_delivery_pickings()
        primary_picking = self._get_unico_primary_delivery_picking(delivery_pickings)
        productions = self._get_unico_mrp_productions()
        promised_datetime = self._get_unico_promised_delivery_datetime()
        delivery_item_count = self._get_unico_delivery_item_count()

        vals = {
            'name': self._get_unico_delivery_task_name(promised_datetime, delivery_item_count),
            'unico_is_local_delivery_task': True,
            'unico_sale_order_id': self.id,
            'partner_id': self.partner_shipping_id.id or self.partner_id.id,
            'unico_customer_id': self.partner_id.id,
            'unico_delivery_partner_id': self.partner_shipping_id.id or self.partner_id.id,
            'unico_warehouse_id': self.warehouse_id.id,
            'unico_promised_delivery_datetime': promised_datetime,
            'unico_effective_delivery_datetime': self.effective_date,
            'unico_incoterm_id': self.incoterm.id,
            'unico_incoterm_location': self.incoterm_location,
            'unico_shipping_policy': self.picking_policy,
            'unico_delivery_status': self.delivery_status,
            'date_deadline': promised_datetime,
            'unico_product_summary': self._get_unico_delivery_product_summary(),
            'unico_delivery_item_count': delivery_item_count,
            'unico_delivery_instructions': self._get_unico_delivery_instructions(),
            'unico_delivery_picking_ids': [Command.set(delivery_pickings.ids)],
            'unico_primary_delivery_picking_id': primary_picking.id,
            'unico_mrp_production_ids': [Command.set(productions.ids)],
        }
        if project:
            vals['project_id'] = project.id
        if create and stage:
            vals['stage_id'] = stage.id
        return vals

    def _get_unico_delivery_project(self):
        project = self.env.ref('unico_delivery_fsm.project_local_door_deliveries', raise_if_not_found=False)
        if project:
            return project.sudo()
        return self.env['project.project'].sudo().search([('name', '=', 'Local Door Deliveries')], limit=1)

    def _get_unico_delivery_pickings(self):
        self.ensure_one()
        return self.picking_ids.filtered(
            lambda picking: picking.state != 'cancel' and picking.location_dest_id.usage == 'customer'
        ).sorted(key=lambda picking: (picking.scheduled_date or fields.Datetime.now(), picking.id))

    def _get_unico_primary_delivery_picking(self, delivery_pickings=None):
        self.ensure_one()
        delivery_pickings = delivery_pickings or self._get_unico_delivery_pickings()
        if not delivery_pickings:
            return self.env['stock.picking']
        warehouse_out = self.warehouse_id.out_type_id
        primary = delivery_pickings.filtered(lambda picking: picking.picking_type_id == warehouse_out)[:1]
        return primary or delivery_pickings[:1]

    def _get_unico_mrp_productions(self):
        self.ensure_one()
        return self.env['mrp.production'].sudo().search([
            '|',
            ('sale_line_id', 'in', self.order_line.ids),
            '&',
            ('sale_line_id', '=', False),
            ('origin', '=', self.name),
        ])

    def _get_unico_deliverable_lines(self):
        self.ensure_one()
        return self.order_line.filtered(
            lambda line: not line.display_type
            and line.product_id
            and line.product_uom_qty > 0
            and line.product_id.type != 'service'
        )

    def _get_unico_delivery_item_count(self):
        self.ensure_one()
        return sum(self._get_unico_deliverable_lines().mapped('product_uom_qty'))

    def _get_unico_delivery_product_summary(self):
        self.ensure_one()
        grouped = OrderedDict()
        for line in self._get_unico_deliverable_lines():
            product_name = line.product_id.display_name or line.name
            if product_name not in grouped:
                grouped[product_name] = 0.0
            grouped[product_name] += line.product_uom_qty
        parts = []
        for product_name, qty in grouped.items():
            qty_label = '%g' % qty
            parts.append('%s x %s' % (qty_label, product_name))
        return '; '.join(parts)

    def _get_unico_delivery_instructions(self):
        self.ensure_one()
        return self.unico_delivery_instructions or False

    def _get_unico_promised_delivery_datetime(self):
        self.ensure_one()
        return self.commitment_date or self.expected_date or False

    def _get_unico_delivery_task_name(self, promised_datetime=False, delivery_item_count=0.0):
        self.ensure_one()
        partner_name = self.partner_shipping_id.display_name or self.partner_id.display_name or 'Customer'
        date_label = ''
        if promised_datetime:
            local_dt = fields.Datetime.context_timestamp(self, promised_datetime)
            date_label = local_dt.strftime('%b %d')
        item_label = '%g items' % delivery_item_count
        parts = [self.name, partner_name]
        if date_label:
            parts.append(date_label)
        parts.append(item_label)
        return ' - '.join(parts)
