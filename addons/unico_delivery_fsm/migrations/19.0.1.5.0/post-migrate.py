# -*- coding: utf-8 -*-
"""Relabel legacy Studio fields that collide, by label, with proper fields on the
same model (res.users/res.partner Resale No., project.task Sales Order and the
delivery FSM fields). Clears ir_model's duplicate-label warnings on the already
installed database without removing any field or its data."""
from odoo import api, SUPERUSER_ID

from odoo.addons.unico_delivery_fsm import _relabel_studio_duplicate_fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _relabel_studio_duplicate_fields(env)
