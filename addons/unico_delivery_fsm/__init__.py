# -*- coding: utf-8 -*-
from . import models


DELIVERY_PROJECT_XMLID = 'unico_delivery_fsm.project_local_door_deliveries'
DELIVERY_PROJECT_NAME = 'Local Door Deliveries'
DELIVERY_STAGE_XMLIDS = (
    ('unico_delivery_fsm.stage_waiting_for_production', 'Waiting for Production'),
    ('unico_delivery_fsm.stage_ready_for_dispatch', 'Ready for Dispatch'),
    ('unico_delivery_fsm.stage_scheduled', 'Scheduled'),
    ('unico_delivery_fsm.stage_out_for_delivery', 'Out for Delivery'),
    ('unico_delivery_fsm.stage_delivered', 'Delivered'),
    ('unico_delivery_fsm.stage_delivery_issue', 'Delivery Issue'),
    ('unico_delivery_fsm.stage_cancelled', 'Cancelled'),
)


# Legacy Studio fields whose label collides with a proper field (a module field
# or another Studio field) on the same model — the source of ir_model's
# "Two fields ... have the same label" warnings. We keep these fields and their
# data and only relabel the obsolete Studio duplicate (append " (Studio)") so the
# labels are unique again. Maps (model, field name) -> new label.
_STUDIO_DUPLICATE_RELABELS = {
    ('res.users', 'x_studio_resale_no_1'): 'Resale No. (Studio)',
    ('res.partner', 'x_studio_resale_no_1'): 'Resale No. (Studio)',
    ('project.task', 'x_studio_sales_order_id'): 'Sales Order (Studio)',
    ('project.task', 'x_studio_delivery_instructions'): 'Delivery Instructions (Studio)',
    ('project.task', 'x_studio_photos_required'): 'Photos Required (Studio)',
    ('project.task', 'x_studio_signature_required'): 'Signature Required (Studio)',
    ('project.task', 'x_studio_issue_type'): 'Issue Type (Studio)',
    ('project.task', 'x_studio_actual_delivery_time'): 'Actual Delivery Time (Studio)',
}


def _relabel_studio_duplicate_fields(env):
    """Relabel legacy Studio fields whose label clashes with a proper field on
    the same model, clearing ir_model's duplicate-label warnings without removing
    any field or its data. Idempotent; only touches manual (Studio) fields."""
    Field = env['ir.model.fields'].sudo()
    for (model, name), label in _STUDIO_DUPLICATE_RELABELS.items():
        field = Field.search([
            ('model', '=', model),
            ('name', '=', name),
            ('state', '=', 'manual'),
        ], limit=1)
        if field and field.field_description != label:
            field.field_description = label


def _post_init_hook(env):
    """Bind XML IDs to any existing Studio prototype project and stages."""
    _relabel_studio_duplicate_fields(env)
    project = env.ref(DELIVERY_PROJECT_XMLID, raise_if_not_found=False)
    existing = env['project.project'].sudo().search([
        ('name', '=', DELIVERY_PROJECT_NAME),
        ('id', '!=', project.id if project else False),
    ], limit=1)

    if existing and project:
        _repoint_xmlid(env, DELIVERY_PROJECT_XMLID, existing)
        _reuse_existing_delivery_stages(env, project, existing)
        if 'active' in project._fields:
            project.active = False
        project = existing

    if project and 'is_fsm' in project._fields:
        project.sudo().write({'is_fsm': True})


def _reuse_existing_delivery_stages(env, generated_project, existing_project):
    Stage = env['project.task.type'].sudo()
    for xmlid, stage_name in DELIVERY_STAGE_XMLIDS:
        generated_stage = env.ref(xmlid, raise_if_not_found=False)
        existing_stage = Stage.search([
            ('name', '=', stage_name),
            ('project_ids', 'in', existing_project.ids),
            ('id', '!=', generated_stage.id if generated_stage else False),
        ], limit=1)

        if existing_stage:
            _repoint_xmlid(env, xmlid, existing_stage)
            if generated_stage:
                generated_stage.unlink()
            continue

        if generated_stage:
            generated_stage.write({
                'project_ids': [
                    (3, generated_project.id),
                    (4, existing_project.id),
                ],
            })


def _repoint_xmlid(env, xmlid, record):
    module, name = xmlid.split('.', 1)
    env['ir.model.data'].sudo().search([
        ('module', '=', module),
        ('name', '=', name),
        ('model', '=', record._name),
    ], limit=1).write({'res_id': record.id})
