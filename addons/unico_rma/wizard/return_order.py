from odoo import api, fields, models
from odoo.exceptions import ValidationError
import logging

log = logging.getLogger(__name__).info


class RmaRejectWizard(models.TransientModel):
    _name = "rma.reject.wizard"
    _description = "Reject RMA"

    rma_id = fields.Many2one("rma.main", string="RMA", required=True, ondelete="cascade")
    reject_reason = fields.Text("Reason for Rejecting", required=True)

    def action_confirm(self):
        self.ensure_one()
        if self.rma_id.rma_type == "legacy_sage":
            self.rma_id._release_legacy_entitlements()
        self.rma_id.write({
            "state": "reject",
            "reject_reason": self.reject_reason,
        })


class ReturnOrderLine(models.TransientModel):
    _name = "return.order.line"
    _description = "Return Order Line"

    return_id = fields.Many2one(string="return order", comodel_name="return.order")
    rma_line_id = fields.Many2one("rma.lines", "RMA Line")
    product_id = fields.Many2one("product.product", "Product")
    original_description = fields.Text(
        related="rma_line_id.original_description",
        string="Original Product Description",
        readonly=True,
    )
    return_scope_label = fields.Char(
        related="rma_line_id.return_scope_label",
        string="Return As",
        readonly=True,
    )
    return_component_summary = fields.Text(
        string="Component Details",
        compute="_compute_return_component_summary",
        compute_sudo=True,
    )
    delivery_qty = fields.Float("Delivered Quantity")
    return_qty = fields.Float("Return Quantity")
    received_qty = fields.Float("Received Quantity")
    sale_line_id = fields.Many2one(string="Order Line", comodel_name="sale.order.line")
    is_new_line = fields.Boolean("new line", default=False)
    pending_qty = fields.Float("Pending Quantity")
    rma_resolution_id = fields.Many2one("rma.resolution", "Return Action")
    return_reason_id = fields.Many2one("return.reason", "Return Reason")
    tracking = fields.Selection(related="product_id.tracking")

    @api.depends("rma_line_id", "return_qty", "delivery_qty")
    def _compute_return_component_summary(self):
        for line in self:
            rma_line = line.rma_line_id
            if not rma_line or not rma_line.sale_line_id:
                line.return_component_summary = False
                continue
            delivery_moves = rma_line._get_original_delivery_moves()
            bom = rma_line._get_return_bom()
            return_scope = rma_line._get_return_scope(delivery_moves, bom)
            basis_qty = line.return_qty if line.return_qty > 0 else line.delivery_qty
            components = rma_line._get_return_component_values(
                return_scope,
                delivery_moves,
                bom,
                basis_qty,
            )
            line.return_component_summary = "\n".join(
                rma_line._format_return_component(component)
                for component in components
            ) or False

    @api.onchange("return_qty")
    def _onchange_return_qty(self):
        self.update({"is_new_line": True})


class RmaReplaceOrderTran(models.TransientModel):
    _name = "return.replace.order"
    _description = "Return Replace Order"

    product_id = fields.Many2one("product.product", string="Product", required=True)
    detailed_type = fields.Selection(related="product_id.type")
    qty = fields.Float("Quantity", default=0.0, digits="Product Unit")
    rma_id = fields.Many2one("return.order", string="RMA Order")
    is_new_line = fields.Boolean("new line", default=False)
    price_unit_manual = fields.Boolean("Manual Price", default=False)
    manual_price_unit = fields.Float("Manual Unit Price", default=0.0, digits="Product Price")
    price_unit = fields.Float(
        string="Unit Price",
        compute="_compute_price_unit",
        inverse="_inverse_price_unit",
        store=True,
        readonly=False,
        digits="Product Price",
    )
    total_price = fields.Float(
        string="Price",
        compute="_compute_total_price",
        store=True,
    )

    @api.onchange("product_id", "qty", "price_unit")
    def _onchange_for_new_line(self):
        self.update({"is_new_line": True})

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for prod in self:
            prod.price_unit_manual = False
            prod.manual_price_unit = 0.0
            if prod.product_id and (prod.product_id.type == "service"):
                prod.qty = 1.0

    @api.onchange("price_unit_manual")
    def _onchange_price_unit_manual(self):
        # Seed the override with the displayed price so opting into manual
        # pricing does not blank the field.
        for prod in self:
            if prod.price_unit_manual:
                prod.manual_price_unit = prod.price_unit

    @api.depends(
        "product_id",
        "product_id.lst_price",
        "qty",
        "price_unit_manual",
        "manual_price_unit",
        "rma_id.rma_order_id",
    )
    def _compute_price_unit(self):
        for prod in self:
            if prod.price_unit_manual:
                prod.price_unit = prod.manual_price_unit
                continue
            rma = prod.rma_id.rma_order_id
            if rma:
                prod.price_unit = rma._replacement_pricelist_price(
                    prod.product_id, prod.qty
                )
            else:
                prod.price_unit = prod.product_id.lst_price if prod.product_id else 0.0

    def _inverse_price_unit(self):
        for prod in self:
            if prod.price_unit_manual:
                prod.manual_price_unit = prod.price_unit

    @api.depends("qty", "price_unit")
    def _compute_total_price(self):
        for prod in self:
            prod.total_price = prod.price_unit * prod.qty


class ReturnOrder(models.TransientModel):
    _name = "return.order"
    _description = "Return Order"

    rma_order_id = fields.Many2one("rma.main", string="rma_order")
    rol_ids = fields.One2many(
        string="return order line",
        comodel_name="return.order.line",
        inverse_name="return_id",
    )
    replace_prd_ids = fields.One2many(
        comodel_name="return.replace.order", inverse_name="rma_id", string="replace_prd"
    )
    is_service = fields.Boolean(
        compute="_compute_is_service", string="is delivery", default=False
    )
    is_without_do_rma = fields.Boolean("Is without do rma")
    rma_type = fields.Selection([("rma_with_do", "Odoo Sale Return")])

    show_replacement_tab = fields.Boolean(
        compute="_compute_show_replacement_tab"
    )

    @api.depends("rol_ids.rma_resolution_id")
    def _compute_show_replacement_tab(self):
        for rec in self:
            rec.show_replacement_tab = any(
                line.rma_resolution_id.rma_action in(
                    'replacement',
                    'replacement_with_returned_item'
                )
                for line in rec.rol_ids
            )




    @api.onchange("replace_prd_ids")
    @api.depends("replace_prd_ids.product_id", "replace_prd_ids.qty")
    def _compute_is_service(self):
        for rplc_id in self:
            replace_prd_id = rplc_id.replace_prd_ids.filtered(
                lambda t: t.product_id.type == "service"
            )
            if replace_prd_id:
                rplc_id.is_service = True
            else:
                rplc_id.is_service = False

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        if self.env.context.get("default_rma_line", False):
            return_order_line_list = []
            rma_lines = self.env["rma.lines"].browse(
                self.env.context.get("default_rma_line", [])
            )
            for i in rma_lines.filtered(
                lambda t: t.product_id.type != "service"
            ):
                vals = {
                    "rma_line_id": i.id,
                    "product_id": i.product_id.id,
                    "delivery_qty": i.delivery_qty,
                    "pending_qty": i.sale_line_id.product_uom_qty
                    - i.sale_line_id.qty_delivered,
                    "return_qty": i.return_qty,
                    "rma_resolution_id": i.rma_resolution_id.id,
                    "return_reason_id": i.return_reason_id.id,
                }

                retun_vals = (0, 0, vals)
                return_order_line_list.append(retun_vals)
            if return_order_line_list != []:
                res.update(
                    {
                        "rol_ids": return_order_line_list,
                    }
                )
        replce_order_line_list = []
        if self.env.context.get("default_replace_prd_ids", []):
            rplc_lines = self.env["rma.replace.order"].browse(
                self.env.context.get("default_replace_prd_ids", [])
            )
            for i in rplc_lines:
                rplcvals = (
                    0,
                    0,
                    {
                        "product_id": i.product_id.id,
                        "qty": i.qty,
                        "price_unit_manual": i.price_unit_manual,
                        "manual_price_unit": i.manual_price_unit,
                    },
                )
                replce_order_line_list.append(rplcvals)
        if replce_order_line_list != []:
            res.update(
                {
                    "replace_prd_ids": replce_order_line_list,
                }
            )
        res.update(
            {
                "rma_order_id": self.env.context.get("default_rma_id"),
                "rma_type": self.env.context.get("default_rma_type"),
            }
        )
        return res

    def check_basic_validation(self):
        if self.rol_ids.filtered(lambda t: t.rma_resolution_id and not t.return_qty):
            raise ValidationError(
                "Sorry! You have to enter return qty for all the RMA reason selected lines"
            )
        if self.rol_ids.filtered(lambda t: not t.rma_resolution_id and t.return_qty):
            raise ValidationError(
                "Sorry! You have to select the resolution for entered returned qty to proceed"
            )
        if self.rol_ids.filtered(
            lambda t: t.rma_resolution_id and not t.return_reason_id
        ):
            raise ValidationError("Sorry! You have to select return reason to proceed")

    def validate_replacement_products(self):
        """This function makes sure that at least on replacement product is added when either the
        'replacement' or 'replacement_with_returned_item' action is selected for any of the RMA lines.
        Raises:
            ValidationError: _description_
        """
        actions = [line.rma_resolution_id.rma_action for line in self.rol_ids]

        if ("replacement" in actions or "replacement_with_returned_item" in actions) and  len(self.replace_prd_ids) == 0:
            raise ValidationError(
                "Please add at least one product to replace under the Replacement Products tab."
            )

    def update_rma_lines(self):

        self.validate_replacement_products()

        if (
            sum(
                self.rol_ids.filtered(
                    lambda t: t.product_id.type != "service"
                ).mapped("return_qty")
            )
            <= 0
        ):
            raise ValidationError(
                "Sorry! You have to enter at-lease one return quantity item to proceed"
            )
        if self.replace_prd_ids:
            if not sum(self.replace_prd_ids.mapped("qty")):
                raise ValidationError(
                    "Sorry! You have to enter at-lease one Quantity for replace product to proceed"
                )
        if self.rma_type == "rma_with_do":
            self.check_basic_validation()
        for i in self.rol_ids:
            if i.is_new_line:
                if not i.rma_line_id.lot_ids:
                    other_rma_return_qty = sum(
                        i.rma_line_id.sale_line_id.return_order_line_id.filtered(
                            lambda x: x.rma_id
                            and x.product_id.id == i.rma_line_id.product_id.id
                            and x.rma_id.state not in ("reject", "processing")
                            and x.id != i.rma_line_id.id
                        ).mapped("return_qty")
                    )
                else:
                    other_rma_return_qty = sum(
                        i.rma_line_id.sale_line_id.return_order_line_id.filtered(
                            lambda x: x.rma_id
                            and x.product_id.id == i.rma_line_id.product_id.id
                            and x.rma_id.state not in ("reject", "processing")
                            and x.id != i.rma_line_id.id
                            and i.rma_line_id.lot_ids[0].id == x.lot_ids[0].id
                        ).mapped("return_qty")
                    )
                if i.delivery_qty < i.rma_line_id.return_qty:
                    raise ValidationError(
                        "Return Quantity is more then Delivered quantity..!!"
                    )
                elif i.return_qty > i.delivery_qty:
                    raise ValidationError(
                        "Return Quantity should not be more then the delivered Qty."
                    )

                elif i.rma_line_id.sale_line_id:
                    if i.return_qty > i.rma_line_id.sale_line_id.qty_delivered:
                        raise ValidationError(
                            "Return Quantity should not be more then the order delivered Qty."
                        )
                if other_rma_return_qty and i.delivery_qty < (
                    other_rma_return_qty + i.return_qty
                ):
                    raise ValidationError(
                        "Total return qty should not more then the order delivered qty"
                    )
                i.rma_line_id.return_qty = i.return_qty
            i.rma_line_id.rma_resolution_id = i.rma_resolution_id.id
            i.rma_line_id.return_reason_id = i.return_reason_id.id

        if self.replace_prd_ids:
            if len(self.replace_prd_ids.filtered(lambda x: x.qty <= 0)):
                raise ValidationError(
                    "Sorry!, We can not proceed with 0 qty, Please enter the quantity to proceed"
                )

            rro_id = self.env["rma.replace.order"].search(
                [("rma_id", "=", self.rma_order_id.id)]
            )
            rro_id.unlink()
            self.env["rma.replace.order"].create([
                {
                    "rma_id": self.rma_order_id.id,
                    "product_id": j.product_id.id,
                    "qty": j.qty,
                    "price_unit_manual": j.price_unit_manual,
                    "manual_price_unit": j.manual_price_unit if j.price_unit_manual else 0.0,
                }
                for j in self.replace_prd_ids
            ])
            self.rma_order_id._update_return_total()
