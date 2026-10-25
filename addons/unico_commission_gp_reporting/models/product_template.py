# -*- coding: utf-8 -*-

from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Odoo's ``copy_data`` reads every ``copy=True`` field with the duplicating
    # user's own rights, so a manager-only field would make Duplicate raise
    # AccessError for everyone else -- including an ordinary Product Manager.
    # These stay ``copy=False`` to keep that read out of the copy path, and
    # ``copy()`` below re-applies them with elevated rights so duplicating an
    # excluded product never quietly produces a commissionable one.
    _UNICO_COMMISSION_GP_COPIED_FIELDS = (
        'unico_commission_gp_excluded',
        'unico_commission_gp_exclusion_note',
    )

    unico_commission_gp_excluded = fields.Boolean(
        string='Exclude from Commissionable GP',
        groups='unico_commission_gp_reporting.group_unico_commission_gp_manager',
        copy=False,
        help=(
            "Exclude this product from commissionable GP reporting by default. "
            "Managers can still manually include individual report lines when needed."
        ),
    )
    unico_commission_gp_exclusion_note = fields.Char(
        string='Commission GP Exclusion Note',
        groups='unico_commission_gp_reporting.group_unico_commission_gp_manager',
        copy=False,
        help="Internal note explaining why this product is excluded from commissionable GP.",
    )

    def copy(self, default=None):
        """Carry the manager-only commission classification onto the copy.

        Runs elevated because the duplicating user may legitimately have no
        Commission GP access at all; an explicit ``default`` still wins, so a
        caller can reclassify the duplicate on the way through.
        """
        copies = super().copy(default=default)
        if isinstance(default, (list, tuple)):
            defaults = list(default)
        else:
            defaults = [default or {}] * len(self)
        for source, copied, record_default in zip(self, copies, defaults):
            source_values = source.sudo()
            carried = {
                name: source_values[name]
                for name in self._UNICO_COMMISSION_GP_COPIED_FIELDS
                if name not in (record_default or {}) and source_values[name]
            }
            if carried:
                copied.sudo().write(carried)
        return copies
