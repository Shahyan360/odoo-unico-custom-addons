import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

log = logging.getLogger(__name__).info


class StockMove(models.Model):
    _inherit = "stock.move"

    rma_line_id = fields.Many2one("rma.lines")
    rma_create_credit_note = fields.Boolean(
        default=False,
        help="Create Credit Note for incoming RMA"
        )

    @api.onchange("quantity")
    def _onchange_quantity_done(self):
        if self.picking_id and self.picking_id.rma_id:
            if self.quantity > self.product_uom_qty:
                raise UserError(_("You can't transfer more than the Initial Demand!"))


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    return_qty = fields.Integer(string="Return Qty")

    @api.onchange("lot_id")
    def validation_for_rma_stock_picking_lot_id(self):
        for stockmoveline in self:
            if (
                stockmoveline.picking_id.rma_id
                and stockmoveline.picking_id.picking_type_id.id
                == stockmoveline.picking_id.picking_type_id.company_id.b2b_source_picking_type_id.id
            ):
                raise ValidationError(
                    "Sorry!, You can not change the lot for the RMA Inwards"
                )
            if (
                stockmoveline.picking_id.rma_id
                and stockmoveline.picking_id.picking_type_id.id
                == stockmoveline.picking_id.picking_type_id.company_id.b2b_without_return_items_picking_type_id.id
            ):
                raise ValidationError("You can not change the lot for the RMA Inwards.")

    @api.onchange("quantity")
    def _onchange_qty_done(self):
        if self.move_id and self.move_id.picking_id and self.move_id.picking_id.rma_id:
            if self.quantity > self.move_id.product_uom_qty:
                raise UserError(_("You can't transfer more than the Initial Demand!"))


class RmaStockPicking(models.Model):
    _inherit = "stock.picking"

    rma_id = fields.Many2one("rma.main", string="RMA ID")
    claim_id = fields.Many2one("rma.claim", string="Claim ID")
    claim_count = fields.Float("Claim Count ", compute="_compute_rma_claim_ids")
    replace_picking_out_rma_count = fields.Integer(
        compute="_replace_picking_out_rma_count"
    )
    rma_action = fields.Selection(
        [
            ("refund", "Refund"),
            ("refund_with_returned_item", "Refund With Returned Items"),
            ("replacement", "Replacement"),
            ("replacement_with_returned_item", "Replacement With Returned Items"),
            ("return_with_credit", "Return item with credit"),
        ],
        string="RMA Resolution",
    )

    def action_to_view_rma(self):
        self.ensure_one()
        rma_id = False

        if self.sale_id and self.sale_id.rma_id:
            rma_id = self.sale_id.rma_id
        if not rma_id and self.rma_id:
            rma_id = self.rma_id
        if rma_id:
            return {
                "name": "RMA",
                "type": "ir.actions.act_window",
                "view_mode": "list,form",
                "res_model": "rma.main",
                "domain": [("id", "=", rma_id.id)],
            }

    def _replace_picking_out_rma_count(self):
        for picking in self:
            picking.replace_picking_out_rma_count = 0
            if picking.sale_id and picking.sale_id.rma_id:
                picking.replace_picking_out_rma_count = 1
            if picking.rma_id:
                picking.replace_picking_out_rma_count = 1

    def _compute_rma_claim_ids(self):
        for order in self:
            rma_claim_ids = self.env["rma.claim"].search(
                [("stock_picking_id", "=", order.id)]
            )
            order.claim_count = len(rma_claim_ids)

    def action_rma_claim_view(self):
        self.ensure_one()
        return {
            "name": "Rma Claim",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "rma.claim",
            "domain": [("stock_picking_id", "=", self.id)],
            "context": {
                "create": False,
            },
        }

    def button_validate(self):
        for picking in self:
            if (
                picking.rma_id
                and picking.rma_id.rma_type == "legacy_sage"
                and picking.picking_type_code == "incoming"
            ):
                picking.rma_id._apply_legacy_manual_values(picking)
        validate_super = super().button_validate()
        for picking in self:
            if not picking.rma_id:
                continue
            picking.rma_id.write({'state': 'processing'})
            if picking.state == 'done' and picking.picking_type_code == 'incoming':
                picking.rma_id._sync_return_to_credit_note(picking)
        return validate_super

    def action_cancel(self):
        cancel_super = super().action_cancel()

        cancel_picking_rma = self.env["rma.main"].search([("id", "=", self.rma_id.id)])

        cancel_picking_rma.update(
            {
                "state": "close",
            }
        )
        return cancel_super
