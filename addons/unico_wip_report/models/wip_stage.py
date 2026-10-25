# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# The tokens the automatic detector can produce. A stage carrying one of these
# is the stage an order lands on when the system detects that situation; a stage
# with no token is manual-only (drag a card onto it, the detector never will).
WIP_AUTO_TOKENS = [
    ("confirmed", "Order Confirmed"),
    ("purchasing", "Purchasing"),
    ("shipping", "Supplier Shipping"),
    ("receipt", "Goods Received"),
    ("delivery", "Out for Delivery"),
]


class UnicoWipStage(models.Model):
    _name = "unico.wip.stage"
    _description = "WIP Lifecycle Stage"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10, index=True)
    description = fields.Char(
        translate=True,
        help="Shown under the column title on the WIP kanban.",
    )
    auto_token = fields.Selection(
        selection=WIP_AUTO_TOKENS,
        string="Automatic Rule",
        index=True,
        help="Which detected situation lands an order on this stage. Leave empty "
             "for a manual-only stage: the detector will never move an order "
             "here, but you can drag one onto it.",
    )
    fold = fields.Boolean(
        string="Folded in Kanban",
        help="Collapse this column by default — useful for stages you only "
             "glance at.",
    )
    is_closed = fields.Boolean(
        string="Closing Stage",
        help="Orders in this stage are treated as finished work: they stay on "
             "the board but are excluded from the open-WIP totals.",
    )
    active = fields.Boolean(default=True)
    item_count = fields.Integer(
        string="Orders", compute="_compute_item_count",
    )

    _sql_constraints = [
        ("unico_wip_stage_name_uniq", "unique(name)",
         "A WIP stage with this name already exists."),
    ]

    def _compute_item_count(self):
        counts = dict(self.env["unico.wip.item"]._read_group(
            [("stage_id", "in", self.ids)], ["stage_id"], ["__count"],
        ))
        for stage in self:
            stage.item_count = counts.get(stage, 0)

    @api.constrains("auto_token", "active")
    def _check_auto_token_unique(self):
        """One stage per automatic rule, otherwise the detector cannot tell which
        stage it means."""
        for stage in self.filtered(lambda s: s.auto_token and s.active):
            clash = self.search([
                ("id", "!=", stage.id),
                ("auto_token", "=", stage.auto_token),
                ("active", "=", True),
            ], limit=1)
            if clash:
                raise ValidationError(_(
                    "Stage %(other)s already handles the automatic rule "
                    "\"%(rule)s\". Two active stages cannot share a rule — the "
                    "detector would not know which one to use.",
                    other=clash.name,
                    rule=dict(WIP_AUTO_TOKENS)[stage.auto_token],
                ))

    @api.model
    def _unico_stage_by_token(self):
        """``{auto_token: stage}`` for the active stages, resolved once per
        batch so the item compute does not search per record."""
        stages = self.search([("auto_token", "!=", False)])
        return {stage.auto_token: stage for stage in stages}

    @api.model
    def _unico_default_stage(self):
        """Fallback when no stage carries the detected rule (someone archived or
        un-tokenised it): the first stage in sequence, so an item is never left
        stageless and invisible on the kanban."""
        return self.search([], limit=1)
