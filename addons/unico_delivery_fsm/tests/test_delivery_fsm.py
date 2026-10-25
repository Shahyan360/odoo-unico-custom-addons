# -*- coding: utf-8 -*-
import re
from datetime import timedelta

from odoo import fields
from odoo.addons.unico_delivery_fsm import DELIVERY_STAGE_XMLIDS, _post_init_hook
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestUnicoDeliveryFsm(TransactionCase):

    @staticmethod
    def _html_text(html_bytes):
        text = html_bytes.decode()
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Delivery Customer',
            'street': '7014 Kari Lane',
            'city': 'Richmond',
            'state_id': cls.env.ref('base.state_us_43').id,
            'zip': '77469',
            'country_id': cls.env.ref('base.us').id,
        })
        cls.shipping_partner = cls.env['res.partner'].create({
            'name': 'Delivery Customer - Job Site',
            'parent_id': cls.partner.id,
            'type': 'delivery',
            'street': '221 Jobsite Way',
            'city': 'Austin',
            'state_id': cls.env.ref('base.state_us_43').id,
            'zip': '78701',
            'country_id': cls.env.ref('base.us').id,
            'phone': '512-555-0100',
        })
        cls.product_door = cls.env['product.product'].create({
            'name': 'Test Door Slab',
            'type': 'consu',
            'list_price': 100.0,
        })
        cls.product_molding = cls.env['product.product'].create({
            'name': 'Test Crown Molding',
            'type': 'consu',
            'list_price': 12.0,
        })
        cls.product_service = cls.env['product.product'].create({
            'name': 'Delivery Consulting',
            'type': 'service',
            'list_price': 50.0,
        })
        cls.incoterm = cls.env['account.incoterms'].search([], limit=1) or cls.env['account.incoterms'].create({
            'name': 'Free Carrier',
            'code': 'FCA',
        })

    def _create_order(self, local_delivery=True, delivery_instructions='Call before arrival. Use side loading dock.',
                      note=False, extra_lines=None, delivery_vals=None):
        order_lines = [
            (0, 0, {
                'product_id': self.product_door.id,
                'product_uom_qty': 2.0,
                'price_unit': 100.0,
            }),
            (0, 0, {
                'product_id': self.product_molding.id,
                'product_uom_qty': 12.0,
                'price_unit': 12.0,
            }),
        ]
        if extra_lines:
            order_lines.extend(extra_lines)
        vals = {
            'partner_id': self.partner.id,
            'partner_shipping_id': self.partner.id,
            'unico_local_delivery_required': local_delivery,
            'unico_delivery_instructions': delivery_instructions,
            'note': note,
            'commitment_date': fields.Datetime.now() + timedelta(days=5),
            'order_line': order_lines,
        }
        if delivery_vals:
            vals.update(delivery_vals)
        order = self.env['sale.order'].create(vals)
        return order

    def test_confirm_local_delivery_order_creates_one_task_without_replacing_stock_flow(self):
        order = self._create_order(local_delivery=True)

        order.action_confirm()

        self.assertTrue(order.picking_ids, 'Native sale_stock delivery orders must still be created.')
        self.assertTrue(order.unico_delivery_task_id, 'A local delivery order should create a delivery task.')

        task = order.unico_delivery_task_id
        self.assertTrue(task.unico_is_local_delivery_task)
        self.assertEqual(task.project_id, self.env.ref('unico_delivery_fsm.project_local_door_deliveries'))
        self.assertEqual(task.stage_id, self.env.ref('unico_delivery_fsm.stage_waiting_for_production'))
        self.assertEqual(task.unico_sale_order_id, order)
        self.assertEqual(task.unico_customer_id, self.partner)
        self.assertEqual(task.unico_delivery_partner_id, self.partner)
        self.assertEqual(task.unico_delivery_status, 'pending')
        self.assertEqual(task.unico_delivery_item_count, 14.0)
        self.assertIn('2 x', task.unico_product_summary)
        self.assertIn('Test Door Slab', task.unico_product_summary)
        self.assertIn('12 x', task.unico_product_summary)
        self.assertIn('Test Crown Molding', task.unico_product_summary)
        self.assertIn('Call before arrival', task.unico_delivery_instructions)
        self.assertEqual(task.unico_delivery_picking_ids, order.picking_ids.filtered(lambda p: p.location_dest_id.usage == 'customer'))

    def test_confirm_non_local_delivery_order_does_not_create_task(self):
        order = self._create_order(local_delivery=False)

        order.action_confirm()

        self.assertTrue(order.picking_ids, 'Native delivery flow should still run.')
        self.assertFalse(order.unico_delivery_task_id)

    def test_incoterm_location_prints_regardless_of_local_delivery(self):
        """The incoterm location note is surfaced on downstream documents whenever
        it is captured, even when the order is not flagged as local delivery."""
        order = self._create_order(
            local_delivery=False,
            delivery_vals={'incoterm_location': 'Gate 4, unload by the west entrance.'},
        )
        self.assertFalse(order.unico_local_delivery_required)
        self.assertEqual(
            order._unico_incoterm_delivery_location(),
            'Gate 4, unload by the west entrance.',
        )

        order.incoterm_location = False
        self.assertFalse(order._unico_incoterm_delivery_location())

    def test_delivery_task_sync_is_idempotent(self):
        order = self._create_order(local_delivery=True)
        order.action_confirm()
        first_task = order.unico_delivery_task_id

        order._sync_unico_delivery_tasks()

        self.assertEqual(order.unico_delivery_task_id, first_task)
        tasks = self.env['project.task'].search([
            ('unico_sale_order_id', '=', order.id),
            ('unico_is_local_delivery_task', '=', True),
        ])
        self.assertEqual(len(tasks), 1)

    def test_marking_confirmed_order_local_creates_delivery_task(self):
        order = self._create_order(local_delivery=False)
        order.action_confirm()
        self.assertFalse(order.unico_delivery_task_id)

        order.write({'unico_local_delivery_required': True})

        self.assertTrue(order.unico_delivery_task_id)
        self.assertTrue(order.unico_delivery_task_id.unico_is_local_delivery_task)

    def test_manual_sync_marks_order_local_and_creates_delivery_task(self):
        order = self._create_order(local_delivery=False)
        order.action_confirm()

        order.action_sync_unico_delivery_task()

        self.assertTrue(order.unico_local_delivery_required)
        self.assertTrue(order.unico_delivery_task_id)

    def test_traceable_mo_updates_existing_delivery_task(self):
        order = self._create_order(local_delivery=True)
        order.action_confirm()
        task = order.unico_delivery_task_id

        line = order.order_line.filtered(lambda sale_line: sale_line.product_id == self.product_door)[:1]
        mo = self.env['mrp.production'].create({
            'product_id': self.product_door.id,
            'product_qty': 2.0,
            'product_uom_id': self.product_door.uom_id.id,
            'sale_line_id': line.id,
            'origin': order.name,
            'location_src_id': order.warehouse_id.lot_stock_id.id,
            'location_dest_id': order.warehouse_id.lot_stock_id.id,
            'picking_type_id': order.warehouse_id.manu_type_id.id,
        })

        self.assertIn(mo, task.unico_mrp_production_ids)

    def test_post_init_reuses_existing_project_and_stages_without_duplicates(self):
        module_project = self.env.ref('unico_delivery_fsm.project_local_door_deliveries')
        module_stage_ids = [self.env.ref(xmlid).id for xmlid, __ in DELIVERY_STAGE_XMLIDS]
        studio_project = self.env['project.project'].create({
            'name': 'Local Door Deliveries',
            'label_tasks': 'Deliveries',
            'privacy_visibility': 'employees',
        })
        studio_stages = self.env['project.task.type'].create([
            {
                'name': stage_name,
                'sequence': index * 10,
                'project_ids': [(4, studio_project.id)],
            }
            for index, (__xmlid, stage_name) in enumerate(DELIVERY_STAGE_XMLIDS, start=1)
        ])

        try:
            _post_init_hook(self.env)

            self.assertEqual(self.env.ref('unico_delivery_fsm.project_local_door_deliveries'), studio_project)
            for stage, (xmlid, stage_name) in zip(studio_stages, DELIVERY_STAGE_XMLIDS):
                self.assertEqual(self.env.ref(xmlid), stage)
                self.assertEqual(stage.name, stage_name)
                self.assertIn(studio_project, stage.project_ids)
            self.assertFalse(self.env['project.task.type'].browse(module_stage_ids).exists())
            self.assertNotIn(module_project, studio_stages.mapped('project_ids'))
            if 'active' in module_project._fields:
                self.assertFalse(module_project.active)
        finally:
            self.env.registry.clear_cache()

    def test_quote_delivery_fields_copy_without_using_quotation_terms_as_instructions(self):
        order = self._create_order(
            local_delivery=True,
            delivery_instructions=False,
            note='<p>All returns require prior authorization.</p>',
            delivery_vals={
                'incoterm': self.incoterm.id,
                'incoterm_location': '140 Smith Street, Austin, Texas',
                'picking_policy': 'one',
            },
        )

        order.action_confirm()

        task = order.unico_delivery_task_id
        self.assertEqual(task.unico_incoterm_id, self.incoterm)
        self.assertEqual(task.unico_incoterm_location, '140 Smith Street, Austin, Texas')
        self.assertEqual(task.unico_shipping_policy, 'one')
        self.assertEqual(task.unico_delivery_status, 'pending')
        self.assertFalse(task.unico_delivery_instructions)

    def test_local_delivery_uses_shipping_partner_and_keeps_incoterm_as_note(self):
        order = self._create_order(
            local_delivery=True,
            delivery_vals={
                'partner_shipping_id': self.shipping_partner.id,
                'incoterm': self.incoterm.id,
                'incoterm_location': 'Gate 4, unload by the west entrance.',
            },
        )

        order.action_confirm()

        task = order.unico_delivery_task_id
        self.assertEqual(task.unico_delivery_partner_id, self.shipping_partner)
        self.assertEqual(task.unico_incoterm_location, 'Gate 4, unload by the west entrance.')

        sale_html, _content_type = self.env['ir.actions.report']._render_qweb_html(
            'sale.action_report_saleorder',
            order.ids,
        )
        sale_text = self._html_text(sale_html)
        self.assertIn('Shipping Address', sale_text)
        self.assertIn('221 Jobsite Way', sale_text)
        self.assertIn('Location Note', sale_text)
        self.assertIn('Gate 4, unload by the west entrance.', sale_text)

        delivery_picking = order.picking_ids.filtered(lambda picking: picking.location_dest_id.usage == 'customer')[:1]
        delivery_html, _content_type = self.env['ir.actions.report']._render_qweb_html(
            'stock.action_report_delivery',
            delivery_picking.ids,
        )
        delivery_text = self._html_text(delivery_html)
        self.assertIn('Shipping Address', delivery_text)
        self.assertIn('221 Jobsite Way', delivery_text)
        self.assertIn('Location Note', delivery_text)
        self.assertIn('Gate 4, unload by the west entrance.', delivery_text)

        picking_html, _content_type = self.env['ir.actions.report']._render_qweb_html(
            'stock.action_report_picking',
            delivery_picking.ids,
        )
        picking_text = self._html_text(picking_html)
        self.assertIn('221 Jobsite Way', picking_text)
        self.assertIn('Location Note', picking_text)
        self.assertIn('Gate 4, unload by the west entrance.', picking_text)

    def test_manual_sync_refreshes_existing_delivery_task_quote_fields(self):
        order = self._create_order(
            local_delivery=True,
            delivery_vals={
                'incoterm': self.incoterm.id,
                'incoterm_location': '140 Smith Street, Austin, Texas',
                'picking_policy': 'one',
            },
        )
        order.action_confirm()
        task = order.unico_delivery_task_id
        task.write({
            'unico_incoterm_id': False,
            'unico_incoterm_location': False,
            'unico_shipping_policy': False,
        })

        order.action_sync_unico_delivery_task()

        self.assertEqual(order.unico_delivery_task_id, task)
        self.assertEqual(task.unico_incoterm_id, self.incoterm)
        self.assertEqual(task.unico_incoterm_location, '140 Smith Street, Austin, Texas')
        self.assertEqual(task.unico_shipping_policy, 'one')

    def test_disabling_local_delivery_cancels_existing_task_without_deleting_history(self):
        order = self._create_order(local_delivery=True)
        order.action_confirm()
        task = order.unico_delivery_task_id

        order.write({'unico_local_delivery_required': False})

        self.assertFalse(order.unico_local_delivery_required)
        self.assertEqual(order.unico_delivery_task_id, task)
        self.assertEqual(task.stage_id, self.env.ref('unico_delivery_fsm.stage_cancelled'))
        cancelled_state = task._unico_get_available_task_state('1_canceled')
        if cancelled_state:
            self.assertEqual(task.state, cancelled_state)

    def test_reenabling_local_delivery_reopens_existing_cancelled_task(self):
        order = self._create_order(local_delivery=True)
        order.action_confirm()
        task = order.unico_delivery_task_id
        order.write({'unico_local_delivery_required': False})

        order.action_sync_unico_delivery_task()

        self.assertTrue(order.unico_local_delivery_required)
        self.assertEqual(order.unico_delivery_task_id, task)
        self.assertEqual(task.stage_id, self.env.ref('unico_delivery_fsm.stage_waiting_for_production'))
        in_progress_state = task._unico_get_available_task_state('01_in_progress')
        if in_progress_state:
            self.assertEqual(task.state, in_progress_state)

    def test_delivery_item_count_excludes_services_and_display_rows(self):
        order = self._create_order(extra_lines=[
            (0, 0, {
                'product_id': self.product_service.id,
                'product_uom_qty': 3.0,
                'price_unit': 50.0,
            }),
            (0, 0, {
                'display_type': 'line_note',
                'name': 'Pack carefully',
            }),
        ])

        order.action_confirm()

        task = order.unico_delivery_task_id
        self.assertEqual(task.unico_delivery_item_count, 14.0)
        self.assertNotIn('Delivery Consulting', task.unico_product_summary)
        self.assertNotIn('Pack carefully', task.unico_product_summary)

    def test_orphan_origin_mo_matches_but_unrelated_sale_linked_mo_does_not(self):
        order = self._create_order(local_delivery=True)
        other_order = self._create_order(local_delivery=False)
        order.action_confirm()
        other_order.action_confirm()
        task = order.unico_delivery_task_id
        other_line = other_order.order_line.filtered(lambda sale_line: sale_line.product_id == self.product_door)[:1]
        unrelated_sale_linked_mo = self.env['mrp.production'].create({
            'product_id': self.product_door.id,
            'product_qty': 1.0,
            'product_uom_id': self.product_door.uom_id.id,
            'sale_line_id': other_line.id,
            'origin': order.name,
            'location_src_id': order.warehouse_id.lot_stock_id.id,
            'location_dest_id': order.warehouse_id.lot_stock_id.id,
            'picking_type_id': order.warehouse_id.manu_type_id.id,
        })
        orphan_mo = self.env['mrp.production'].create({
            'product_id': self.product_door.id,
            'product_qty': 1.0,
            'product_uom_id': self.product_door.uom_id.id,
            'origin': order.name,
            'location_src_id': order.warehouse_id.lot_stock_id.id,
            'location_dest_id': order.warehouse_id.lot_stock_id.id,
            'picking_type_id': order.warehouse_id.manu_type_id.id,
        })

        order._sync_unico_delivery_tasks()

        self.assertIn(orphan_mo, task.unico_mrp_production_ids)
        self.assertNotIn(unrelated_sale_linked_mo, task.unico_mrp_production_ids)

    def test_cancelled_delivery_picking_is_removed_from_task(self):
        order = self._create_order(local_delivery=True)
        order.action_confirm()
        task = order.unico_delivery_task_id
        picking = task.unico_delivery_picking_ids[:1]
        self.assertTrue(picking)

        picking.action_cancel()

        self.assertNotIn(picking, task.unico_delivery_picking_ids)
        self.assertFalse(task.unico_primary_delivery_picking_id)
