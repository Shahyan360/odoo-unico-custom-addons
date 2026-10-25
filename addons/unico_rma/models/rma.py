import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import formatLang

log = logging.getLogger(__name__).info

RMA_PRODUCT_UOM_DECIMAL_PRECISION = "Product Unit"


class RmaMain(models.Model):
    _name = "rma.main"
    _description = "RMA"
    _inherit = [
        "mail.thread",
        "mail.activity.mixin",
    ]
    _order = "date desc, id desc"

    name = fields.Char("Name", default=lambda self: _("New"), store=True)
    sale_order = fields.Many2one(
        "sale.order", "Sale Order", domain="[('state','in',['sale','done'])]"
    )
    sale_order_ids = fields.One2many(
        string="Order Lines", related="sale_order.order_line"
    )
    subject = fields.Char("Subject")
    date = fields.Datetime("Date", default=datetime.now(), required=True)
    deadline = fields.Datetime("Deadline", default=datetime.now(), required=True)
    rma_note = fields.Text("RMA Note")
    priority = fields.Selection(
        [("0", "No Priority"), ("1", "Low"), ("2", "Normal"), ("3", "High")], "Priority"
    )
    responsible = fields.Many2one("res.users", "Responsible", store=True)
    sales_channel = fields.Many2one("crm.team", "Sales Channel", store=True)
    delivery_order = fields.Many2one(
        "stock.picking",
        "Delivery Order",
        store=True,
        domain="[('picking_type_code','=','outgoing'), ('sale_id','=',sale_order), ('state', 'in', ['assigned', 'done'])]",
    )
    del_email = fields.Char("Delivery Email", store=True)
    partner_id = fields.Many2one(
        "res.partner",
        "Customer",
        compute="_compute_partner_id",
        store=True,
        readonly=False,
    )
    del_partner = fields.Many2one("res.partner", "Delivery Partner", store=True)
    del_phone = fields.Char("Delivery Phone", store=True)
    del_street = fields.Char("Street")
    del_street2 = fields.Char("Street2")
    del_city = fields.Char("City")
    del_zip = fields.Char("Zip")
    del_state_id = fields.Many2one(
        "res.country.state",
        string="Del State",
        domain="[('country_id', '=?', country_id)]",
    )
    del_country_id = fields.Many2one("res.country", string="Del Country")
    inv_partner = fields.Many2one("res.partner", "Invoice Partner", store=True)
    inv_email = fields.Char("Invoice Email", store=True)
    inv_phone = fields.Char("Invoice Phone", store=True)
    inv_street = fields.Char("Inv Street")
    inv_street2 = fields.Char("Inv Street2")
    inv_city = fields.Char("Inv City")
    inv_zip = fields.Char("Inv Zip")
    inv_state_id = fields.Many2one(
        "res.country.state", string="State", domain="[('country_id', '=?', country_id)]"
    )
    inv_country_id = fields.Many2one("res.country", string="Inv Country")
    del_phone = fields.Char("Phone", store=True)
    del_email = fields.Char("Email", store=True)
    rma_line_ids = fields.One2many("rma.lines", "rma_id", "RMA Lines", store=True)
    return_reason = fields.Char("Return Reason")

    in_delivery_count = fields.Integer(
        string="Incoming Orders", compute="_compute_incoming_picking_ids"
    )
    out_delivery_count = fields.Integer(
        string="Outgoing Orders", compute="_compute_outgoing_picking_ids"
    )
    refund_inv_count = fields.Integer(
        string="Customer Credit", compute="_compute_refund_inv_ids"
    )
    sale_order_count = fields.Integer(
        string="Sale Order Count", compute="_compute_sale_order_ids"
    )
    company_id = fields.Many2one(
        "res.company", string="Company", default=lambda self: self.env.company
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Warehouse",
        compute="_compute_warehouse_id",
        store=True,
        readonly=False,
        check_company=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submit", "Submitted"),
            ("approved", "Approved"),
            ("processing", "Processing"),
            ("close", "Closed"),
            ("reject", "Rejected"),
            ("cancel", "Cancelled"),
        ],
        string="Status",
        default="draft",
        tracking=True,
    )
    replace_prd_ids = fields.One2many(
        "rma.replace.order", "rma_id", string="Replacement Products"
    )
    total_return = fields.Float(
        "Total Return", compute="_update_return_total", default=0.0, store=True
    )
    total_replace = fields.Float(
        "Total Replacement", compute="_update_replace_total", default=0.0, store=True
    )
    total_difference = fields.Float(
        "Total Difference", compute="_update_difference_total", store=True
    )
    difference_amount = fields.Float(
        "Difference Amount", compute="_update_difference_total", store=True
    )
    remarks = fields.Text("Remarks")
    validated = fields.Boolean("Validate", compute="_compute_validated")
    reject_reason = fields.Text("Reject Reason", readonly=True, copy=False)

    # Customer payment(s) released by a "return_with_credit" resolution.
    credit_released_payment_ids = fields.Many2many(
        "account.payment",
        "rma_released_payment_rel",
        "rma_id",
        "payment_id",
        string="Released Payments",
        copy=False,
    )
    credit_currency_id = fields.Many2one(
        "res.currency", compute="_compute_credit_available_amount"
    )
    credit_available_amount = fields.Monetary(
        string="Credit Available",
        compute="_compute_credit_available_amount",
        currency_field="credit_currency_id",
        help="Outstanding customer credit released by this RMA's "
             "return-with-credit resolution that is still available to apply "
             "to a new invoice.",
    )

    # Manual override for the restocking fee, set directly on the RMA rather
    # than derived from a line's resolution. When non-zero it replaces the
    # resolution-driven fee(s) entirely — see _return_fee_amounts's callers,
    # which all check this first so the credit note, its preview, and the
    # fee label can never disagree on which fee actually applies.
    manual_restock_fee = fields.Monetary(
        string="Manual Restock Fee",
        currency_field="credit_preview_currency_id",
        help="Restocking fee to charge on this RMA specifically, overriding "
             "whatever the line resolutions would otherwise compute. Leave "
             "at 0 to use the resolution-based fee(s) instead.",
    )

    # Approval-stage breakdown of the credit note this RMA will produce.
    # Display only — see _compute_credit_preview.
    credit_preview_currency_id = fields.Many2one(
        "res.currency", compute="_compute_credit_preview"
    )
    credit_preview_untaxed = fields.Monetary(
        string="Credit Subtotal",
        compute="_compute_credit_preview",
        currency_field="credit_preview_currency_id",
        help="Value of the returned items after any discount, less the return "
             "fee — the credit note's untaxed amount.",
    )
    credit_preview_fee = fields.Monetary(
        string="Restocking Fee",
        compute="_compute_credit_preview",
        currency_field="credit_preview_currency_id",
        help="Restocking fee withheld from the credit, set by the resolution "
             "on each line. Negative because it reduces what is credited.",
    )
    credit_preview_fee_label = fields.Char(
        string="Fee Basis", compute="_compute_credit_preview"
    )
    credit_preview_has_fee = fields.Boolean(
        compute="_compute_credit_preview",
        help="True when a restocking fee will be withheld. Drives the "
             "no-fee notice, so the approver is told either way instead of "
             "having to read the absence of a line as 'no fee'.",
    )
    credit_preview_tax = fields.Monetary(
        string="Tax",
        compute="_compute_credit_preview",
        currency_field="credit_preview_currency_id",
        help="Tax on the returned items, carried over from the original sale "
             "order. Sage returns are untaxed unless Tax Handling says "
             "otherwise.",
    )
    credit_preview_total = fields.Monetary(
        string="Credit to Customer",
        compute="_compute_credit_preview",
        currency_field="credit_preview_currency_id",
        help="What the customer is credited in total, tax included.",
    )
    credit_preview_is_estimate = fields.Boolean(
        compute="_compute_credit_preview",
        help="True while the credit note does not exist yet, so the figures "
             "are still an estimate based on the requested return quantities.",
    )
    process_outcome_notice = fields.Char(
        string="Processing Outcome",
        compute="_compute_process_outcome_notice",
        help="What processing this RMA will produce, given the resolution "
             "chosen on each line: an incoming return transfer, a customer "
             "credit note, both, or neither.",
    )

    is_without_do_rma = fields.Boolean("IS without do rma")
    rma_resolution_id = fields.Many2one("rma.resolution", "RMA Resolution")

    rma_type = fields.Selection([
        ("rma_with_do", "Odoo Sale Return"),
        ("legacy_sage", "Sage Return"),
    ])
    is_editable = fields.Boolean(compute="_compute_is_editable", default=True)
    legacy_sage_order_ref = fields.Char("Original Sage Order #", copy=False)
    legacy_sage_invoice_ref = fields.Char("Original Sage Invoice #", copy=False)
    legacy_sage_invoice_date = fields.Date("Original Sage Invoice Date", copy=False)
    legacy_duplicate_reference_override = fields.Boolean(
        "Allow Same Sage Reference Again",
        copy=False,
        groups="sales_team.group_sale_manager",
        help="Manager approval when the same Sage order or invoice must be used on more than one RMA.",
    )
    legacy_accounting_approved = fields.Boolean(
        "Accounting Approved Credit",
        copy=False,
        tracking=True,
        help="Accounting must approve before this Sage return credit can be posted.",
    )
    legacy_tax_treatment = fields.Selection(
        [
            ("no_odoo_tax", "Do Not Add Odoo Tax"),
            ("selected_odoo_tax", "Use Selected Taxes"),
            ("manual_adjustment", "Manual Tax Adjustment"),
        ],
        string="Tax Handling",
        default="no_odoo_tax",
        required=True,
        copy=False,
    )
    legacy_ar_treatment = fields.Selection(
        [
            ("migrated_ar", "Apply to Imported Customer Balance"),
            ("standalone_review", "Create Credit for Review"),
        ],
        string="Credit Handling",
        default="standalone_review",
        required=True,
        copy=False,
    )
    legacy_source_location_id = fields.Many2one(
        "stock.location",
        string="Return Stock Source",
        default=lambda self: self.env.ref(
            "unico_rma.stock_location_legacy_sage_customer_returns",
            raise_if_not_found=False,
        ),
        copy=False,
    )
    legacy_return_location_id = fields.Many2one(
        "stock.location",
        string="Return Destination",
        default=lambda self: self._default_legacy_return_location_id(),
        copy=False,
        check_company=True,
    )
    legacy_picking_type_id = fields.Many2one(
        "stock.picking.type",
        string="Return Receipt Operation",
        default=lambda self: self._default_legacy_picking_type_id(),
        copy=False,
        check_company=True,
    )
    legacy_replacement_pricing_policy = fields.Selection(
        [
            ("zero_warranty", "No-Charge Warranty"),
            ("discounted_exchange", "Discounted Replacement"),
            ("billable", "Bill Customer"),
        ],
        string="Replacement Pricing",
        default="zero_warranty",
        required=True,
        copy=False,
    )
    legacy_replacement_discount = fields.Float(
        "Replacement Discount %",
        copy=False,
        help="Used only when Replacement Pricing is Discounted Replacement.",
    )

    @api.constrains("legacy_replacement_discount")
    def _check_legacy_replacement_discount(self):
        for rma in self:
            if rma.legacy_replacement_discount < 0 or rma.legacy_replacement_discount > 100:
                raise ValidationError(_("Replacement discount must be between 0 and 100."))

    def _default_legacy_picking_type_id(self):
        company = self.env.company
        return self.env["stock.picking.type"].search(
            [("code", "=", "incoming"), ("company_id", "=", company.id)], limit=1
        )

    def _default_legacy_return_location_id(self):
        picking_type = self._default_legacy_picking_type_id()
        return (
            picking_type.default_location_dest_id
            or self.env["stock.warehouse"].search(
                [("company_id", "=", self.env.company.id)], limit=1
            ).lot_stock_id
        )

    @api.depends("sale_order.partner_id")
    def _compute_partner_id(self):
        for rma in self:
            if rma.sale_order:
                rma.partner_id = rma.sale_order.partner_id
            else:
                rma.partner_id = rma.partner_id.id if rma.partner_id else False

    @api.depends("sale_order.warehouse_id")
    def _compute_warehouse_id(self):
        for rma in self:
            if rma.sale_order:
                rma.warehouse_id = rma.sale_order.warehouse_id
            else:
                rma.warehouse_id = rma.warehouse_id.id if rma.warehouse_id else False

    def action_cancel(self):
        self.ensure_one()
        if self.rma_type == "legacy_sage":
            self._release_legacy_entitlements()
        # Cancel direct + chained pickings (3-step reception adds QC/STOR)
        pickings = self._get_all_rma_pickings().filtered(
            lambda p: p.state != "cancel"
        )
        if pickings:
            pickings.action_cancel()

        # Cancel related replacement sale orders. In Odoo 19 sale.order's
        # public action_cancel returns a confirmation wizard; _action_cancel
        # performs the cancellation directly.
        sale_orders = self.env["sale.order"].search(
            [("origin", "=", self.name), ("state", "!=", "cancel")]
        )
        if sale_orders:
            sale_orders._action_cancel()

        # Cancel draft credit notes for this RMA
        credit_notes = self.env["account.move"].search(
            [
                ("rma_id", "=", self.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "draft"),
            ]
        )
        if credit_notes:
            credit_notes.button_cancel()

        self.write({"state": "cancel"})

    def action_reject_wizard(self):
        self.ensure_one()
        return {
            "name": "Reject RMA",
            "type": "ir.actions.act_window",
            "view_mode": "form",
            "res_model": "rma.reject.wizard",
            "target": "new",
            "context": {"default_rma_id": self.id},
        }

    def _compute_is_editable(self):
        for rma in self:
            rma.is_editable = True

    @api.depends(
        "rma_line_ids.price_unit",
        "rma_line_ids.return_qty",
    )
    def _update_return_total(self):
        for case in self:
            total = 0
            for line in case.rma_line_ids:
                total += line.total_price
            case.total_return = total

    @api.depends("replace_prd_ids.total_price")
    def _update_replace_total(self):
        for case in self:
            total = 0
            for line in case.replace_prd_ids:
                total += line.total_price
            case.total_replace = total

    @api.depends("total_return", "total_replace")
    def _update_difference_total(self):
        self.total_difference = 0.00
        self.difference_amount = 0.00
        for rma in self:
            rma.total_difference = rma.total_replace - rma.total_return
            rma.difference_amount = abs(rma.total_difference)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("rma_type") in ("rma_with_do", "legacy_sage"):
                vals.update(
                    {
                        "name": self.env["ir.sequence"].next_by_code("rma.order"),
                    }
                )
        return super().create(vals_list)

    # @api.depends('sale_order', 'sale_order.order_line')
    # def _compute_order_line_ids(self):
    #     """Update order lines when sale order changes"""
    #     for rma in self:
    #         if rma.sale_order:
    #             # Get all order lines from the sale order
    #             # rma.order_line_ids = rma.sale_order.order_line

    #             # Or filter specific lines (e.g., only delivered products)
    #             rma.rma_line_ids = rma.sale_order.order_line.filtered(
    #                 lambda l: l.qty_delivered > 0
    #             )
    #         else:
    #             rma.rma_line_ids = False

    @api.onchange("sale_order")
    def onchange_sale_order_id(self):
        if self.sale_order and self.rma_type == "rma_with_do":
            self.partner_id = self.sale_order.partner_id
            self.warehouse_id = self.sale_order.warehouse_id
            self.delivery_order = self.sale_order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing" and p.state == "done"
            )[:1]
            self._set_delivery_partner_details(
                self.delivery_order.partner_id or self.sale_order.partner_shipping_id
            )

            order_line_list = []
            for line in self.rma_line_ids:
                self.rma_line_ids = [(2, line.id, 0)]

            for i in self.sale_order.order_line:
                if i.product_id.type != "service":
                    order_line_list.append((0, 0, {
                        "product_id": i.product_id.id,
                        "delivery_qty": i.product_uom_qty,
                        "sale_line_id": i.id,
                        "price_unit": i.product_id.standard_price,
                    }))

            self.rma_line_ids = order_line_list
        elif self.rma_type == "legacy_sage":
            self.sale_order = False
            self.delivery_order = False
            self._set_delivery_partner_details(False)
            self._set_invoice_partner_details(False)

    def action_submit(self):
        if self.rma_type == "legacy_sage":
            self._validate_legacy_sage_submit()
            self.write({"state": "submit"})
            return

        if not sum(
            self.rma_line_ids.filtered(
                lambda t: t.product_id.type != "service"
            ).mapped("return_qty")
        ):
            raise ValidationError(
                "Sorry! You have to enter at-lease one return item to proceed"
            )

        if self.replace_prd_ids:
            if not sum(self.replace_prd_ids.mapped("qty")):
                raise ValidationError(
                    "Sorry! You have to enter at-lease one qty for replace product to proceed"
                )

        service_line = self.rma_line_ids.filtered(
            lambda t: t.product_id.type == "service"
        )
        rma_lines = self.rma_line_ids.filtered(
            lambda t: t.product_id.type != "service"
            and t.rma_resolution_action
        )
        rma_resolution_actions = set(rma_lines.mapped("rma_resolution_action"))
        if (
            service_line
            and len(rma_resolution_actions) == 1
            and service_line[0].rma_resolution_id
            and service_line[0].rma_resolution_id.id
            != rma_lines[0].rma_resolution_id.id
        ):
            raise ValidationError(
                "Please select correct Resolution for shipping item and then proceed"
            )

        if self.rma_type == "legacy_sage":
            self._validate_legacy_sage_submit()

        self.write({"state": "submit"})

    def _get_all_rma_pickings(self):
        """All pickings linked to this RMA: ones with rma_id set directly,
        plus any pickings auto-chained downstream by the warehouse routes
        (e.g. WH/IN → WH/QC → WH/STOR in 3-step reception). Walks the move
        chain via move_dest_ids until no new pickings are discovered."""
        self.ensure_one()
        Picking = self.env["stock.picking"]
        direct = Picking.search([("rma_id", "=", self.id)])
        all_pickings = direct
        frontier_moves = direct.move_ids
        seen = set(direct.ids)
        while frontier_moves:
            next_moves = frontier_moves.move_dest_ids
            next_pickings = next_moves.picking_id.filtered(lambda p: p.id not in seen)
            if not next_pickings:
                break
            all_pickings |= next_pickings
            seen.update(next_pickings.ids)
            frontier_moves = next_moves
        return all_pickings

    def _compute_incoming_picking_ids(self):
        for order in self:
            order.in_delivery_count = len(order._get_all_rma_pickings())

    def _compute_sale_order_ids(self):
        # TODO: Come back to this
        for order in self:
            sale_order_ids = self.env["sale.order"].search(
                [("origin", "=", order.name)]
            )
            order.sale_order_count = len(sale_order_ids)

    def _compute_outgoing_picking_ids(self):
        for order in self:
            stock_picking_ids = self.env["stock.picking"].search(
                [("rma_id", "=", order.id), ("picking_type_code", "=", "outgoing")]
            )
            order.out_delivery_count = len(stock_picking_ids)

    def _compute_refund_inv_ids(self):
        for inv in self:
            refund_inv_ids = self.env["account.move"].search([("rma_id", "=", inv.id)])
            inv.refund_inv_count = len(refund_inv_ids)

    def _compute_validated(self):
        for rma in self:
            if rma.state == "processing":
                pickings = rma._get_all_rma_pickings()
                rma.validated = not pickings.filtered(
                    lambda t: t.state not in ("done", "cancel")
                )
            else:
                rma.validated = False

    def action_return_replace(self):
        ctx = {
            "default_rma_id": self.id,
            "default_rma_type": self.rma_type,
            "default_rma_line": self.rma_line_ids.ids,
            "default_replace_prd_ids": self.replace_prd_ids.ids,
        }
        return {
            "type": "ir.actions.act_window",
            "view_type": "form",
            "name": "RMA Resolution",
            "view_mode": "form",
            "res_model": "return.order",
            "target": "new",
            "context": ctx,
        }

    @api.onchange("sale_order")
    def set_sale_details(self):
        sale_order_obj = self.env["sale.order"].search(
            [("id", "=", self.sale_order.id)]
        )
        self.sales_channel = sale_order_obj.team_id
        self.responsible = sale_order_obj.user_id.id
        self._set_invoice_partner_details(sale_order_obj.partner_invoice_id)
        self._set_delivery_partner_details(
            self.delivery_order.partner_id or sale_order_obj.partner_shipping_id
        )

    def _set_delivery_partner_details(self, partner):
        self.del_partner = partner
        self.del_phone = partner.phone if partner else False
        self.del_email = partner.email if partner else False
        self.del_street = partner.street if partner else False
        self.del_street2 = partner.street2 if partner else False
        self.del_city = partner.city if partner else False
        self.del_zip = partner.zip if partner else False
        self.del_state_id = partner.state_id if partner else False
        self.del_country_id = partner.country_id if partner else False

    def _set_invoice_partner_details(self, partner):
        self.inv_partner = partner
        self.inv_phone = partner.phone if partner else False
        self.inv_email = partner.email if partner else False
        self.inv_street = partner.street if partner else False
        self.inv_street2 = partner.street2 if partner else False
        self.inv_city = partner.city if partner else False
        self.inv_zip = partner.zip if partner else False
        self.inv_state_id = partner.state_id if partner else False
        self.inv_country_id = partner.country_id if partner else False

    @api.onchange("delivery_order")
    def update_delivery_details(self):
        order_line_list = []
        order_lines = []
        for rma in self:
            partner_id = (
                rma.delivery_order.partner_id
                or rma.sale_order.partner_shipping_id
            )
            rma._set_delivery_partner_details(partner_id)
            for line in rma.rma_line_ids:
                rma.rma_line_ids = [(2, line.id, 0)]
            if rma.rma_type == "rma_with_do":
                # Build RMA lines from the SO; pull lot info and stock move
                # linkage from the chosen outgoing picking so lot-tracked
                # returns and 3-step flows keep their move/lot references.
                picking = rma.delivery_order
                for line in self.sale_order_ids:
                    if line.product_id.type == "service" or not line.qty_delivered:
                        continue
                    move = picking.move_ids.filtered(
                        lambda m: m.sale_line_id.id == line.id
                    )[:1]
                    move_lines = move.move_line_ids if move else False
                    lot_ids = move_lines.lot_id.ids if move_lines else []
                    order_lines.append((0, 0, {
                        "product_id": line.product_id.id,
                        "delivery_qty": line.qty_delivered,
                        "sale_line_id": line.id,
                        "stock_move_id": move.id if move else False,
                        "move_line_id": move_lines[:1].id if move_lines else False,
                        "lot_ids": [(6, 0, lot_ids)] if lot_ids else False,
                        "price_unit": line.price_unit,
                    }))
        self.rma_line_ids = order_lines

    def rma_line_btn(self):
        self.ensure_one()
        return {
            "name": "Product",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "product.product",
            "domain": [("rma_id", "=", self.id)],
        }

    @api.onchange("deadline", "date")
    def _onchange_deadline(self):
        if self.deadline and self.date:
            if self.date > self.deadline:
                raise UserError(_("Please select a proper date."))

    def action_send_rma(self):
        self.ensure_one()

        ir_model_data = self.env["ir.model.data"]
        try:
            template_id = ir_model_data._xmlid_lookup("unico_rma.email_template_edi_rma")[
                1
            ]
        except ValueError:
            template_id = False
        try:
            compose_form_id = ir_model_data._xmlid_lookup(
                "mail.email_compose_message_wizard_form"
            )[1]
        except ValueError:
            compose_form_id = False
        ctx = {
            "default_model": "rma.main",
            "default_res_ids": self.ids,
            "default_use_template": bool(template_id),
            "default_template_id": template_id,
            "default_composition_mode": "comment",
            "force_email": True,
        }
        return {
            "type": "ir.actions.act_window",
            "view_type": "form",
            "view_mode": "form",
            "res_model": "mail.compose.message",
            "views": [(compose_form_id, "form")],
            "view_id": compose_form_id,
            "target": "new",
            "context": ctx,
        }

    def process_rma(self):
        if self.rma_type == "legacy_sage":
            return self._process_legacy_sage_rma()

        refund_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "refund"
        )
        refund_with_return_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "refund_with_returned_item"
        )
        replacement_with_return_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "replacement_with_returned_item"
        )
        return_with_credit_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "return_with_credit"
        )

        # "return_with_credit" frees an already-collected payment, which may
        # have been taken before the order was invoiced (e.g. a portal or
        # terminal transaction). Only enforce the "must be invoiced first"
        # rule when there are other actions that genuinely need an invoice.
        if self.rma_line_ids - return_with_credit_items:
            self.validate_invoice()

        credit_note_items = (
            refund_items | refund_with_return_items | replacement_with_return_items
        )
        # "return_with_credit" also receives the goods back like the
        # "*_with_returned_item" actions, but instead of a credit note it
        # frees the customer's already-collected payment (see
        # _process_return_with_credit).
        returns = (
            refund_with_return_items
            | replacement_with_return_items
            | return_with_credit_items
        )
        replacements = self.replace_prd_ids

        if credit_note_items:
            self.process_credit_note(credit_note_items)

        # Free the collected payment (and, for a full return, cancel the sale
        # order + invoice) BEFORE creating the incoming return transfer.
        # Cancelling the sale order also cancels its not-done pickings, so the
        # return transfer must be created afterwards or it would be cancelled
        # along with the order.
        if return_with_credit_items:
            self._process_return_with_credit(return_with_credit_items)

        if returns:
            self.process_returned_items(returns)

        if replacements:
            self.process_replacement_items(replacements)

        return True

    def _validate_legacy_sage_submit(self):
        self.ensure_one()
        if self.sale_order or self.delivery_order:
            raise UserError(_("Sage returns should not be linked to an Odoo sale or delivery order."))
        if not self.partner_id:
            raise UserError(_("Choose the customer for this Sage return."))
        if not (self.legacy_sage_invoice_ref or self.legacy_sage_order_ref):
            raise UserError(_("Enter the original Sage order or invoice number for this return."))
        self._check_legacy_sage_reference_unique()

        active_lines = self.rma_line_ids.filtered(
            lambda line: line.product_id.type != "service"
            and (line.return_qty > 0 or line.rma_resolution_action in (
                "replacement",
                "replacement_with_returned_item",
            ))
        )
        replacement_qty = sum(self.replace_prd_ids.mapped("qty"))
        if not active_lines and not replacement_qty:
            raise UserError(_("Add at least one returned item or replacement item."))

        missing_resolution = active_lines.filtered(lambda line: not line.rma_resolution_id)
        if missing_resolution:
            raise UserError(_("Choose what should happen for each returned item."))

        missing_reason = active_lines.filtered(lambda line: not line.return_reason_id)
        if missing_reason:
            raise UserError(_("Choose a return reason for each returned item."))

        missing_entitlement = active_lines.filtered(lambda line: not line.legacy_entitlement_id)
        if missing_entitlement:
            raise UserError(_("Choose the original Sage sale line for each returned item."))
        active_lines._apply_legacy_entitlement_defaults()

        wrong_customer = active_lines.filtered(
            lambda line: line.legacy_entitlement_id.partner_id.commercial_partner_id
            != self.partner_id.commercial_partner_id
        )
        if wrong_customer:
            raise UserError(_("The selected original Sage sale line belongs to a different customer."))

        wrong_product = active_lines.filtered(
            lambda line: line.legacy_entitlement_id.product_id != line.product_id
        )
        if wrong_product:
            raise UserError(_("The returned product must match the selected original Sage sale line."))

        over_entitlement = active_lines.filtered(
            lambda line: line.return_qty > line._get_legacy_entitlement_available_qty()
        )
        if over_entitlement:
            raise UserError(
                _(
                    "Return quantity is greater than the quantity left to return from the original Sage sale line."
                )
            )

        if self.replace_prd_ids and not replacement_qty:
            raise UserError(_("Enter a replacement quantity before submitting."))

    def _check_legacy_sage_reference_unique(self):
        self.ensure_one()
        if self.legacy_duplicate_reference_override:
            return
        reference_domain = []
        if self.legacy_sage_invoice_ref:
            reference_domain = [("legacy_sage_invoice_ref", "=", self.legacy_sage_invoice_ref)]
        elif self.legacy_sage_order_ref:
            reference_domain = [("legacy_sage_order_ref", "=", self.legacy_sage_order_ref)]
        if not reference_domain:
            return
        duplicate = self.search([
            ("id", "!=", self.id),
            ("company_id", "=", self.company_id.id),
            ("rma_type", "=", "legacy_sage"),
            ("state", "not in", ("reject", "cancel")),
        ] + reference_domain, limit=1)
        if duplicate:
            raise UserError(
                _(
                    "Sage reference %(reference)s is already used on %(rma)s. "
                    "A sales manager must allow using the same Sage reference again."
                ) % {
                    "reference": self.legacy_sage_invoice_ref or self.legacy_sage_order_ref,
                    "rma": duplicate.name,
                }
            )

    def _validate_legacy_sage_processing(self, credit_note_items, return_items):
        self._validate_legacy_sage_submit()
        replacement_actions = self.rma_line_ids.filtered(
            lambda line: line.rma_resolution_action in (
                "replacement",
                "replacement_with_returned_item",
            )
        )
        if replacement_actions and not self.replace_prd_ids:
            raise UserError(_("Add replacement products before approving this Sage return."))

        if credit_note_items:
            missing_account = credit_note_items.filtered(
                lambda line: not line.legacy_refund_account_id
            )
            if missing_account:
                raise UserError(
                    _(
                        "Choose a credit account for every item that will create a customer credit."
                    )
                )
            if self.legacy_tax_treatment == "selected_odoo_tax":
                missing_tax = credit_note_items.filtered(
                    lambda line: not line.legacy_tax_ids
                )
                if missing_tax:
                    raise UserError(
                        _(
                            "Choose taxes for every credit line, or change Tax Handling."
                        )
                    )

        if return_items:
            if not self.legacy_picking_type_id:
                raise UserError(_("Choose the return receipt operation."))
            if not self.legacy_source_location_id:
                raise UserError(_("Choose where the returned stock is coming from."))
            if not self.legacy_source_location_id.valuation_account_id:
                raise UserError(
                    _(
                        "Set a stock valuation clearing account on the return stock source before approval."
                    )
                )
            if not self.legacy_return_location_id:
                raise UserError(_("Select the return destination location."))

            non_storable = return_items.filtered(lambda line: not line.product_id.is_storable)
            if non_storable:
                raise UserError(
                    _(
                        "Returned physical items must use storable products so Odoo can value the receipt."
                    )
                )

            missing_cost = return_items.filtered(lambda line: line.legacy_unit_cost < 0)
            if missing_cost:
                raise UserError(_("Original unit cost cannot be negative."))
            self._validate_legacy_return_lots(return_items)

    def _validate_legacy_return_lots(self, return_items):
        for line in return_items.filtered(lambda item: item.product_id.tracking != "none"):
            if not line.lot_ids:
                raise UserError(
                    _("Enter lot or serial numbers for %s before approving this return.")
                    % line.product_id.display_name
                )
            if line.product_id.tracking == "serial":
                if line.return_qty != int(line.return_qty):
                    raise UserError(_("Serial-numbered returns must use whole-number quantities."))
                if len(line.lot_ids) != int(line.return_qty):
                    raise UserError(
                        _("Enter one serial number for each returned unit.")
                    )
            elif line.product_id.tracking == "lot" and len(line.lot_ids) != 1:
                raise UserError(_("Enter one lot number for this returned item."))

    def _process_legacy_sage_rma(self):
        self.ensure_one()
        refund_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "refund"
        )
        refund_with_return_items = self.rma_line_ids.filtered(
            lambda t: t.rma_resolution_action == "refund_with_returned_item"
        )
        credit_note_items = refund_items | refund_with_return_items
        return_items = refund_with_return_items
        replacement_items = self.replace_prd_ids

        self._validate_legacy_sage_processing(credit_note_items, return_items)
        self._consume_legacy_entitlements()

        if return_items:
            self._process_legacy_return_receipt(return_items)

        if credit_note_items:
            self._process_legacy_credit_note(credit_note_items)

        if replacement_items:
            self._process_legacy_replacement_items(replacement_items)

        return True

    def _consume_legacy_entitlements(self):
        self.ensure_one()
        for line in self.rma_line_ids.filtered("legacy_entitlement_id"):
            delta = line.return_qty - line.legacy_entitlement_consumed_qty
            if delta > 0:
                line.legacy_entitlement_id.sudo().consume_return_qty(delta)
                line.legacy_entitlement_consumed_qty += delta

    def _release_legacy_entitlements(self):
        self.ensure_one()
        for line in self.rma_line_ids.filtered("legacy_entitlement_id"):
            if line.legacy_entitlement_consumed_qty > 0:
                line.legacy_entitlement_id.sudo().release_return_qty(
                    line.legacy_entitlement_consumed_qty
                )
                line.legacy_entitlement_consumed_qty = 0.0

    def _get_legacy_credit_note_line_vals(self, line):
        self.ensure_one()
        tax_ids = []
        if self.legacy_tax_treatment == "selected_odoo_tax":
            tax_ids = line.legacy_tax_ids.ids

        return {
            "product_id": line.product_id.id,
            "name": line._get_legacy_credit_line_name(),
            "quantity": line.return_qty,
            "price_unit": line.price_unit,
            "account_id": line.legacy_refund_account_id.id,
            "tax_ids": [(6, 0, tax_ids)],
            "rma_line_id": line.id,
        }

    def _process_legacy_credit_note(self, refund_items):
        self.ensure_one()
        account_move_lines = [
            (0, 0, self._get_legacy_credit_note_line_vals(line))
            for line in refund_items
            if line.return_qty > 0
        ]
        if not account_move_lines:
            return False

        credit_note_vals = {
            "move_type": "out_refund",
            "partner_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "rma_id": self.id,
            "invoice_origin": self.legacy_sage_invoice_ref
            or self.legacy_sage_order_ref
            or self.name,
            "invoice_date": fields.Date.context_today(self),
            "invoice_user_id": self.env.uid,
            "narration": self._get_legacy_credit_note_narration(),
            "currency_id": self.company_id.currency_id.id,
            "invoice_line_ids": account_move_lines,
        }
        journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.company_id.id)],
            limit=1,
        )
        if journal:
            credit_note_vals["journal_id"] = journal.id

        credit_note = self.env["account.move"].search(
            [
                ("rma_id", "=", self.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "draft"),
            ],
            limit=1,
        )
        if credit_note:
            credit_note.write({"invoice_line_ids": [(5, 0, 0)] + account_move_lines})
            return credit_note
        return self.env["account.move"].create(credit_note_vals)

    def _get_legacy_credit_note_narration(self):
        self.ensure_one()
        return "\n".join(
            value
            for value in (
                self.name,
                _("Original Sage order: %s") % self.legacy_sage_order_ref
                if self.legacy_sage_order_ref
                else False,
                _("Original Sage invoice: %s") % self.legacy_sage_invoice_ref
                if self.legacy_sage_invoice_ref
                else False,
                _("Original Sage invoice date: %s") % self.legacy_sage_invoice_date
                if self.legacy_sage_invoice_date
                else False,
                _("Tax handling: %s") % dict(self._fields["legacy_tax_treatment"].selection).get(
                    self.legacy_tax_treatment
                ),
                _("Credit handling: %s") % dict(self._fields["legacy_ar_treatment"].selection).get(
                    self.legacy_ar_treatment
                ),
            )
            if value
        )

    def _process_legacy_return_receipt(self, return_lines):
        self.ensure_one()
        picking_vals = {
            "picking_type_id": self.legacy_picking_type_id.id,
            "partner_id": self.partner_id.id,
            "origin": self.legacy_sage_invoice_ref
            or self.legacy_sage_order_ref
            or self.name,
            "location_id": self.legacy_source_location_id.id,
            "location_dest_id": self.legacy_return_location_id.id,
            "rma_id": self.id,
            "company_id": self.company_id.id,
        }
        picking = self.env["stock.picking"].create(picking_vals)
        for line in return_lines.filtered(lambda l: l.return_qty > 0):
            move = self.env["stock.move"].create(
                self._prepare_legacy_stock_move_vals(line, picking)
            )
            move.value_manual = line.legacy_unit_cost * line.return_qty
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids.filtered(lambda m: m.rma_line_id.lot_ids):
            move.lot_ids = [(6, 0, move.rma_line_id.lot_ids.ids)]
        return picking

    def _prepare_legacy_stock_move_vals(self, line, picking):
        self.ensure_one()
        return {
            "product_id": line.product_id.id,
            "product_uom_qty": line.return_qty,
            "product_uom": line.product_id.uom_id.id,
            "picking_id": picking.id,
            "picking_type_id": self.legacy_picking_type_id.id,
            "location_id": self.legacy_source_location_id.id,
            "location_dest_id": self.legacy_return_location_id.id,
            "company_id": self.company_id.id,
            "rma_line_id": line.id,
        }

    def _apply_legacy_manual_values(self, picking):
        self.ensure_one()
        if self.rma_type != "legacy_sage":
            return
        for move in picking.move_ids.filtered(lambda m: m.rma_line_id):
            qty = move.quantity or move.product_uom_qty
            move.value_manual = move.rma_line_id.legacy_unit_cost * qty

    def validate_invoice(self):
        # ==================== CHECK FIRST IF THE INVOICE WAS ISSUED FOR THE SALE ORDER =========================
        sale_invoices = self.env["account.move"].search(
            [
                ("invoice_origin", "=", self.sale_order.name),
                ("move_type", "=", "out_invoice"),
                ("state", "!=", "cancel"),
            ]
        )
        if not sale_invoices:
            raise UserError(
                "No invoice found for the related Sale Order.\n Please invoice the Sale Order first, then proceed with RMA."
            )

    def _return_fee_amounts(self, refund_items):
        """Return [(resolution, fee_amount)] for every resolution among
        ``refund_items`` that has apply_return_fee=True and yields a
        positive fee.

        Percentage fees are computed against the subtotal of the lines
        that share that resolution, so the fee scales with what is
        actually being refunded.

        Resolution-based fee maths only: both the credit-note lines
        (:meth:`_compute_return_fee_lines`) and the approval-stage preview
        (:meth:`_compute_credit_preview`) call this, but only after first
        checking ``manual_restock_fee`` — when that override is set, this
        method is skipped entirely and the manual amount is used instead,
        so the figure shown before approval still cannot drift from what
        gets posted."""
        amounts = []
        # Group refund items by their resolution
        for resolution in refund_items.mapped("rma_resolution_id").filtered(
            "apply_return_fee"
        ):
            res_items = refund_items.filtered(
                lambda l, r=resolution: l.rma_resolution_id == r
            )
            if resolution.return_fee_type == "percentage":
                subtotal = sum(l.return_qty * l.price_unit for l in res_items)
                fee_amount = subtotal * (resolution.return_fee_value / 100.0)
            else:  # "amount"
                fee_amount = resolution.return_fee_value
            if fee_amount <= 0:
                continue
            amounts.append((resolution, fee_amount))
        return amounts

    def _compute_return_fee_lines(self, refund_items):
        """Build the Return Fee credit-note line(s) for refund items whose
        resolution has apply_return_fee=True — or, if manual_restock_fee is
        set, a single line for that instead.

        Returns a list of (0, 0, vals) tuples, each with quantity=-1 so the
        fee reduces the credit note total (refund minus fee). The fee
        value is positive."""
        self.ensure_one()
        Product = self.env["product.product"]
        fee_product = self.env.ref(
            "unico_rma.product_return_fee", raise_if_not_found=False
        ) or Product
        if not fee_product:
            return []

        if self.manual_restock_fee:
            return [(0, 0, {
                "product_id": fee_product.id,
                "name": _("Return Fee (Manual)"),
                "quantity": -1.0,
                "price_unit": self.manual_restock_fee,
            })]

        return [
            (0, 0, {
                "product_id": fee_product.id,
                "name": f"Return Fee ({resolution.rma_resolution})",
                "quantity": -1.0,
                "price_unit": fee_amount,
            })
            for resolution, fee_amount in self._return_fee_amounts(refund_items)
        ]

    # ------------------------------------------------------------------
    #  Credit preview (approval stage)
    # ------------------------------------------------------------------
    # The RMA form used to show only ``total_return`` — a bare
    # qty x price subtotal with no tax and no restocking fee — so an
    # approver had no way to tell what the customer would actually be
    # credited until the credit note already existed. The fields below
    # mirror the credit-note maths for display only; nothing here changes
    # what gets posted.
    def _credit_note_items(self):
        """RMA lines that end up on the credit note.

        Mirrors the ``credit_note_items`` sets built in :meth:`process_rma`
        and :meth:`_process_legacy_sage_rma` — legacy Sage RMAs do not
        credit ``replacement_with_returned_item`` lines."""
        self.ensure_one()
        actions = ["refund", "refund_with_returned_item"]
        if self.rma_type != "legacy_sage":
            actions.append("replacement_with_returned_item")
        return self.rma_line_ids.filtered(
            lambda l: l.rma_resolution_action in actions and l.return_qty > 0
        )

    def _credit_line_taxes(self, line):
        """Taxes the credit note will carry for ``line``.

        Odoo returns use the original sale line's taxes
        (:meth:`process_credit_note`); Sage returns use the manually
        chosen ``legacy_tax_ids``, and only when Tax Handling says so
        (:meth:`_get_legacy_credit_note_line_vals`)."""
        self.ensure_one()
        if self.rma_type == "legacy_sage":
            if self.legacy_tax_treatment == "selected_odoo_tax":
                return line.legacy_tax_ids
            return self.env["account.tax"]
        return line.sale_line_id.tax_ids

    @api.depends(
        "rma_line_ids.return_qty",
        "rma_line_ids.price_unit",
        "rma_line_ids.rma_resolution_action",
        "rma_line_ids.rma_resolution_id",
        "rma_line_ids.legacy_tax_ids",
        "legacy_tax_treatment",
        "rma_type",
        "manual_restock_fee",
    )
    def _compute_credit_preview(self):
        """Itemise what the customer will be credited: subtotal, restocking
        fee, tax, and the resulting net.

        Once a credit note exists the figures are read straight off it, so
        an approved RMA never shows a preview that disagrees with the
        document it produced."""
        for rma in self:
            rma.credit_preview_currency_id = rma.company_id.currency_id
            # Same search-based lookup as _compute_refund_inv_ids: the credit
            # notes are linked by rma_id, not by a stored o2m on this model.
            # _origin so an unsaved record being edited in the form (NewId)
            # falls through to the estimate instead of searching on a fake id.
            origin_id = rma._origin.id
            posted = self.env["account.move"].search([
                ("rma_id", "=", origin_id),
                ("move_type", "=", "out_refund"),
                ("state", "!=", "cancel"),
            ]) if origin_id else self.env["account.move"]
            if posted:
                rma.credit_preview_untaxed = sum(posted.mapped("amount_untaxed"))
                rma.credit_preview_tax = sum(posted.mapped("amount_tax"))
                rma.credit_preview_total = sum(posted.mapped("amount_total"))
                rma.credit_preview_fee = -rma._posted_fee_total(posted)
                rma.credit_preview_fee_label = (
                    _("Manual Restock Fee") if rma.manual_restock_fee
                    else rma._fee_label(
                        rma._return_fee_amounts(rma._credit_note_items())
                    )
                )
                rma.credit_preview_has_fee = bool(rma.credit_preview_fee)
                rma.credit_preview_is_estimate = False
                continue

            items = rma._credit_note_items()
            if rma.manual_restock_fee:
                fee_total = rma.manual_restock_fee
                fee_label = _("Manual Restock Fee")
            else:
                fees = rma._return_fee_amounts(items)
                fee_total = sum(amount for _resolution, amount in fees)
                fee_label = rma._fee_label(fees)

            untaxed = 0.0
            tax = 0.0
            for line in items:
                # The credit note copies the sale line's discount, so the
                # taxable base is the discounted price, not the raw one.
                discount = (
                    0.0 if rma.rma_type == "legacy_sage"
                    else line.sale_line_id.discount
                )
                price = line.price_unit * (1 - (discount or 0.0) / 100.0)
                taxes = rma._credit_line_taxes(line)
                if taxes:
                    result = taxes.compute_all(
                        price,
                        currency=rma.company_id.currency_id,
                        quantity=line.return_qty,
                        product=line.product_id,
                        partner=rma.partner_id,
                    )
                    untaxed += result["total_excluded"]
                    tax += result["total_included"] - result["total_excluded"]
                else:
                    untaxed += price * line.return_qty

            # The fee posts as an untaxed negative line, so it reduces the
            # subtotal and the net but never the tax.
            rma.credit_preview_untaxed = untaxed - fee_total
            rma.credit_preview_fee = -fee_total
            rma.credit_preview_tax = tax
            rma.credit_preview_total = untaxed - fee_total + tax
            rma.credit_preview_fee_label = fee_label
            rma.credit_preview_has_fee = bool(fee_total)
            rma.credit_preview_is_estimate = True

    # ------------------------------------------------------------------
    #  Processing outcome notice (draft/submitted stage)
    # ------------------------------------------------------------------
    # Which documents an RMA produces is decided line by line, by the
    # resolution's action — so "Refund" credits without taking goods back,
    # "Replacement" takes nothing back and credits nothing, and only the
    # "*_with_returned_item" actions do both. That is not readable off the
    # lines table, so state it in one sentence above it.
    def _rma_processing_outcomes(self):
        """Documents processing this RMA will create, as display labels.

        Mirrors the branching in :meth:`process_rma` and
        :meth:`_process_legacy_sage_rma`, and reuses :meth:`_credit_note_items`
        so the notice can never disagree with the credit figures shown in the
        footer."""
        self.ensure_one()
        legacy = self.rma_type == "legacy_sage"
        returned_actions = ["refund_with_returned_item"]
        if not legacy:
            # A Sage return only receives goods back on a refund-with-return;
            # an Odoo return also does for replacements and credit releases.
            returned_actions += [
                "replacement_with_returned_item", "return_with_credit"]

        outcomes = []
        goods_back = self.rma_line_ids.filtered(
            lambda line: line.rma_resolution_action in returned_actions
            and line.return_qty > 0
        )
        if goods_back:
            outcomes.append(_("an incoming return transfer"))
        if self._credit_note_items():
            outcomes.append(_("a customer credit note"))
        if not legacy and self.rma_line_ids.filtered(
            lambda line: line.rma_resolution_action == "return_with_credit"
            and line.return_qty > 0
        ):
            # No credit note: the collected payment is freed instead.
            outcomes.append(_("a release of the payment already collected"))
        if self.replace_prd_ids:
            outcomes.append(_("a replacement sales order"))
        return outcomes

    @api.depends(
        "rma_type",
        "rma_line_ids.return_qty",
        "rma_line_ids.rma_resolution_action",
        "replace_prd_ids",
    )
    def _compute_process_outcome_notice(self):
        for rma in self:
            outcomes = rma._rma_processing_outcomes()
            if outcomes:
                rma.process_outcome_notice = _(
                    "Processing this RMA will create: %s.", ", ".join(outcomes))
            else:
                rma.process_outcome_notice = _(
                    "As set up, processing this RMA creates neither a return "
                    "transfer nor a credit note. Set a resolution and a return "
                    "quantity on each line.")

    def _posted_fee_total(self, credit_notes):
        """Total of the Return Fee lines already on ``credit_notes``."""
        fee_product = self.env.ref(
            "unico_rma.product_return_fee", raise_if_not_found=False
        )
        if not fee_product:
            return 0.0
        fee_lines = credit_notes.invoice_line_ids.filtered(
            lambda l: l.product_id == fee_product
        )
        # Fee lines carry quantity -1, so their subtotal is negative.
        return abs(sum(fee_lines.mapped("price_subtotal")))

    def _fee_label(self, fees):
        """Human-readable breakdown, e.g. "Wrong Item 10%; Damaged $50.00"."""
        parts = []
        for resolution, _amount in fees:
            if resolution.return_fee_type == "percentage":
                rate = f"{resolution.return_fee_value:g}%"
            else:
                rate = formatLang(
                    self.env,
                    resolution.return_fee_value,
                    currency_obj=self.company_id.currency_id,
                )
            parts.append(f"{resolution.rma_resolution} {rate}")
        return "; ".join(parts)

    def process_credit_note(self, refund_items):
        """This function creates a credit note for the refund

        Args:
            refund_items (_type_): RMA lines to be refunded
        """
        account_move_lines = []

        # ==================== ACCOUNT MOVE LINES FOR REFUND =========================
        for line in refund_items:
            account_move_line = (
                0,
                0,
                {
                    "product_id": line.product_id.id,
                    "name": line.original_description or line.product_id.display_name,
                    "quantity": line.return_qty,
                    "price_unit": line.price_unit,
                    "discount": line.sale_line_id.discount,
                    "tax_ids": line.sale_line_id.tax_ids,
                    "rma_line_id": line.id,
                    # "sale_line_id": line.sale_line_id.id,
                },
            )
            account_move_lines.append(account_move_line)

        # ==================== RETURN FEE LINES (qty -1, reduces refund) ===============
        account_move_lines.extend(self._compute_return_fee_lines(refund_items))

        # ==================== UPDATE CREDIT NOTE IF ALREADY EXISTS =========================
        credit_note = self.env["account.move"].search(
            [
                ("rma_id", "=", self.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "draft"),
            ]
        )

        if credit_note:
            credit_note.write({"invoice_line_ids": account_move_lines})
        else:
            # ==================== CREATE ACCOUNT MOVE FOR REFUND IF CREDIT NOTE DOES NOT EXIST =========================
            sale_id = self.sale_order
            fiscal_position_id = (
                self.env["account.fiscal.position"]
                .with_company(sale_id.company_id)
                ._get_fiscal_position(sale_id.partner_id)
            )

            account_move = {
                "move_type": "out_refund",
                "partner_id": self.partner_id.id,
                "sale_id": sale_id.id,
                "rma_id": self.id,
                "invoice_origin": sale_id.name,
                "fiscal_position_id": fiscal_position_id.id
                if fiscal_position_id
                else False,
                "team_id": sale_id.team_id.id,
                "invoice_date": self.deadline or False,
                "invoice_user_id": self.env.uid,
                "narration": self.name,
                "currency_id": sale_id.company_id.currency_id.id,
                "invoice_line_ids": account_move_lines,
                "delivery_date": sale_id.commitment_date,
            }

            self.env["account.move"].create(account_move)

    # ==================================================================
    #  RETURN ITEM WITH CREDIT
    # ==================================================================
    def _receivable_payment_lines(self, payment):
        """The receivable/payable journal-item(s) of a payment move."""
        return payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type
            in ("asset_receivable", "liability_payable")
        )

    @api.depends(
        "credit_released_payment_ids",
        "credit_released_payment_ids.state",
        "credit_released_payment_ids.is_reconciled",
    )
    def _compute_credit_available_amount(self):
        """Live outstanding credit still available from the released
        payment(s). Shrinks as the credit is re-applied to new invoices,
        so the banner disappears once it is fully used."""
        for rma in self:
            currency = (
                rma.credit_released_payment_ids[:1].currency_id
                or rma.company_id.currency_id
            )
            total = 0.0
            for payment in rma.credit_released_payment_ids:
                lines = rma._receivable_payment_lines(payment)
                total += abs(sum(lines.mapped("amount_residual")))
            rma.credit_currency_id = currency
            rma.credit_available_amount = total

    def _return_with_credit_value(self, return_lines):
        """Tax-included value of the items being returned.

        Matched against the tax-included amount the customer actually paid
        so we can tell a full return (whole order coming back) from a
        partial one. Uses the original sale order line's tax-included total
        per unit when available, falling back to the RMA line price."""
        total = 0.0
        for line in return_lines:
            if line.return_qty <= 0:
                continue
            sale_line = line.sale_line_id
            if sale_line and sale_line.product_uom_qty:
                unit_incl = sale_line.price_total / sale_line.product_uom_qty
            else:
                unit_incl = line.price_unit
            total += unit_incl * line.return_qty
        return total

    def _process_return_with_credit(self, return_lines):
        """Free the customer's already-collected payment for a
        "return_with_credit" resolution instead of issuing a credit note.

        The returned goods are still received back (the lines are part of
        the ``returns`` set in :meth:`process_rma`, so a return picking is
        created). What differs from a refund is the money: rather than
        crediting the customer, the payment they already paid is released
        so it can be re-applied to a new invoice.

        Behaviour:

        * Every inbound customer payment collected for the order (terminal,
          portal or manual - resolved via the Stripe-terminal
          ``_unico_customer_payments_map`` helper) is unreconciled from the
          order's invoices and detached from the sale order, turning it
          into an available outstanding credit.
        * **Full return** (returned value covers the whole order): the
          posted customer invoice(s) and the sale order are cancelled, and
          the freed payment stays available to reconcile against a new
          invoice.
        * **Partial return**: the invoice is reopened and a note records
          the returned vs. kept amount so the freed credit can be
          re-applied manually via the invoice reconcile widget. We do not
          split a single payment automatically because there is no safe
          native API to free an exact sub-amount of a payment."""
        self.ensure_one()
        order = self.sale_order
        if not order:
            raise UserError(
                _("A sale order is required to process a return with credit.")
            )

        returned_value = self._return_with_credit_value(return_lines)
        if returned_value <= 0:
            return

        payments = order._unico_customer_payments_map().get(order.id)
        if not payments:
            order.message_post(
                body=_(
                    "RMA %s: return with credit requested but no collected "
                    "customer payment was found to release."
                )
                % self.name
            )
            return

        currency = order.currency_id or order.company_id.currency_id
        full_return = currency.compare_amounts(returned_value, order.amount_total) >= 0

        freed = self.env["account.payment"]
        for payment in payments:
            lines = self._receivable_payment_lines(payment)
            if lines.filtered("reconciled"):
                lines.remove_move_reconcile()
            # Detach the payment from this sale order so it is no longer
            # tied to it and can be assigned to a new invoice.
            if "sale_order_id" in payment._fields and payment.sale_order_id == order:
                payment.sale_order_id = False
            freed |= payment

        # Detach any online/portal transactions from the order so the
        # freed payment is not automatically re-linked.
        for tx in order.transaction_ids.filtered(
            lambda t: t.payment_id and t.payment_id in freed
        ):
            tx.sale_order_ids = [(3, order.id)]

        # Remember the released payments so the "credit available" banner on
        # the RMA and the sale order can track how much is still usable.
        self.credit_released_payment_ids = [(6, 0, freed.ids)]

        freed_total = sum(freed.mapped("amount"))
        if full_return:
            self._cancel_sale_and_invoices(order)
            order.message_post(
                body=_(
                    "RMA %(rma)s - return with credit (full return): released "
                    "%(amount)s in customer payment(s) and cancelled the sale "
                    "order and its invoice(s). The payment(s) are now available "
                    "to reconcile against a new invoice."
                )
                % {
                    "rma": self.name,
                    "amount": formatLang(
                        self.env, freed_total, currency_obj=currency
                    ),
                }
            )
        else:
            order.message_post(
                body=_(
                    "RMA %(rma)s - return with credit (partial return): released "
                    "%(freed)s in customer payment(s); returned value is "
                    "%(returned)s. Re-apply the returned portion to the new "
                    "invoice and the kept portion (%(kept)s) back to %(order)s's "
                    "invoice from the reconcile widget."
                )
                % {
                    "rma": self.name,
                    "freed": formatLang(self.env, freed_total, currency_obj=currency),
                    "returned": formatLang(
                        self.env, returned_value, currency_obj=currency
                    ),
                    "kept": formatLang(
                        self.env,
                        max(freed_total - returned_value, 0.0),
                        currency_obj=currency,
                    ),
                    "order": order.name,
                }
            )

    def _cancel_sale_and_invoices(self, order):
        """Cancel the order's posted customer invoices and the sale order.

        Called for a full return-with-credit once the collected payment
        has been unreconciled, so drafting/cancelling the invoices no
        longer trips over an existing reconciliation."""
        invoices = order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        for invoice in invoices:
            try:
                invoice.button_draft()
                invoice.button_cancel()
            except Exception as error:  # pragma: no cover - defensive
                log(
                    "RMA %s: could not cancel invoice %s: %s"
                    % (self.name, invoice.name, error)
                )
        # In Odoo 19 sale.order's public action_cancel returns a confirmation
        # wizard; _action_cancel performs the cancellation directly (same
        # pattern used by action_cancel above).
        try:
            order._action_cancel()
        except Exception as error:  # pragma: no cover - defensive
            log("RMA %s: could not cancel sale order %s: %s" % (self.name, order.name, error))

    def _sync_return_to_credit_note(self, picking):
        """Back-update the verified received quantity from a done return
        picking onto the matching rma.lines and any draft credit note,
        then re-apply the restocking fee so it reflects the actual amount
        being credited.

        Without this, a returned-item RMA leaves the credit note line at
        the originally-requested return_qty even after the warehouse
        verifies a smaller amount, so the percentage-based restocking fee
        is computed on the wrong subtotal.
        """
        self.ensure_one()
        if picking.rma_id != self:
            return
        if picking.state != "done" or picking.picking_type_code != "incoming":
            return

        # Sum done quantities across all incoming return pickings so that
        # partial returns split across multiple pickings still produce
        # the right total per rma.line.
        all_return_pickings = self._get_all_rma_pickings().filtered(
            lambda p: p.state == "done" and p.picking_type_code == "incoming"
        )
        received_by_line = {}
        for p in all_return_pickings:
            for move in p.move_ids.filtered(
                lambda m: m.state == "done" and m.rma_line_id
            ):
                received_by_line[move.rma_line_id.id] = (
                    received_by_line.get(move.rma_line_id.id, 0.0) + move.quantity
                )

        if not received_by_line:
            return

        draft_credit_notes = self.env["account.move"].search(
            [
                ("rma_id", "=", self.id),
                ("move_type", "=", "out_refund"),
                ("state", "=", "draft"),
            ]
        )

        touched_cn = self.env["account.move"]
        for rma_line_id, received_qty in received_by_line.items():
            rma_line = self.env["rma.lines"].browse(rma_line_id)
            if rma_line.return_qty == received_qty:
                continue
            rma_line._sync_legacy_entitlement_consumed_qty(received_qty)
            rma_line.write(
                {
                    "received_qty": received_qty,
                    "return_qty": received_qty,
                }
            )
            for cn in draft_credit_notes:
                cn_lines = cn.invoice_line_ids.filtered(
                    lambda l, r=rma_line: l.rma_line_id == r
                )
                if cn_lines:
                    cn_lines.write({"quantity": received_qty})
                    touched_cn |= cn

        for cn in touched_cn:
            self._sync_credit_note_fee_lines(cn)

    def _sync_credit_note_fee_lines(self, credit_note):
        """Drop existing return-fee lines from credit_note and rebuild
        them from the current rma.lines so the fee scales with the
        verified quantities. Idempotent."""
        self.ensure_one()
        fee_product = self.env.ref(
            "unico_rma.product_return_fee", raise_if_not_found=False
        )
        if not fee_product:
            return

        existing_fee_lines = credit_note.invoice_line_ids.filtered(
            lambda l: l.product_id == fee_product
        )
        if existing_fee_lines:
            existing_fee_lines.unlink()

        credit_items = self.rma_line_ids.filtered(
            lambda l: l.rma_resolution_action in (
                "refund",
                "refund_with_returned_item",
                "replacement_with_returned_item",
            )
        )
        new_lines = self._compute_return_fee_lines(credit_items)
        if new_lines:
            credit_note.write({"invoice_line_ids": new_lines})

    def process_returned_items(self, return_lines):
        """This function creates an incoming shipment for returned items on an RMA
                using Odoo core return picking wizard

        Args:
            return_lines (_type_): _Items to be returned.
        """
        self.ensure_one()

        if not self.delivery_order:
            raise UserError(_("No delivery order linked to this RMA."))

        return_items = return_lines.filtered(lambda item: item.return_qty > 0)

        if not return_items:
            raise UserError(_("No return quantities provided"))

        # One sale order can ship as several deliveries — a configured door
        # leaves from Post-Production while stocked goods leave from Output —
        # and the RMA header only points at one of them. Returning is
        # per-picking, so each line has to go back to the delivery that
        # actually shipped it rather than to whichever one the header names.
        for picking, picking_lines in self._group_return_lines_by_delivery(
            return_items
        ).items():
            self._create_return_for_delivery(picking, picking_lines)

    def _group_return_lines_by_delivery(self, return_items):
        """Map each delivery to the RMA lines it actually shipped.

        Raises when a line was never shipped by any outgoing transfer, which
        is the only case where there is genuinely nothing to receive back.
        """
        self.ensure_one()
        lines_by_picking = {}
        unshipped = self.env["rma.lines"]
        for item in return_items:
            picking = self._find_delivery_for_return_line(item)
            if not picking:
                unshipped |= item
                continue
            lines_by_picking.setdefault(picking, self.env["rma.lines"])
            lines_by_picking[picking] |= item
        if unshipped:
            raise UserError(_(
                "These RMA lines were never shipped on any delivery for "
                "%(order)s, so there is nothing to receive back:\n%(lines)s",
                order=self.sale_order.name or "-",
                lines="\n".join(
                    "- %s" % (line.product_id.display_name or "?")
                    for line in unshipped
                ),
            ))
        return lines_by_picking

    def _find_delivery_for_return_line(self, rma_line):
        """The outgoing transfer that shipped ``rma_line``.

        Prefers the RMA's own delivery order when it really carries the line,
        so nothing changes for single-delivery orders, and otherwise follows
        the sale order line to whichever delivery did ship it.
        """
        self.ensure_one()
        sale_line = rma_line.sale_line_id
        if not sale_line:
            # Nothing to trace the line by; the header's delivery is the only
            # candidate, and _fill_return_wizard_line will report it if wrong.
            return self.delivery_order
        shipped_moves = self.env["stock.move"].search([
            ("sale_line_id", "=", sale_line.id),
            ("state", "=", "done"),
            ("picking_id.picking_type_id.code", "=", "outgoing"),
        ])
        if not shipped_moves:
            return self.env["stock.picking"]
        if self.delivery_order in shipped_moves.picking_id:
            return self.delivery_order
        return shipped_moves.picking_id[:1]

    def _create_return_for_delivery(self, picking_id, return_items):
        """Build and confirm one return transfer for ``picking_id``."""
        self.ensure_one()
        picking_type_id = (
            self.sale_order.warehouse_id.out_type_id.return_picking_type_id
        )

        wizard = (
            self.env["stock.return.picking"]
            .with_context(
                active_model="stock.picking",
                active_id=picking_id.id,
                active_ids=[picking_id.id],
            )
            .create({})
        )

        unmatched = self.env["rma.lines"]
        # Delivery move -> the rma.lines it was filled for, recorded as we go.
        # Re-deriving this afterwards from the products would repeat the very
        # guesswork that broke configured items in the first place.
        rma_line_by_delivery_move = {}
        for item in return_items:
            matched, linkable_lines = self._fill_return_wizard_line(wizard, item)
            if not matched:
                unmatched |= item
                continue
            for wizard_line in linkable_lines:
                rma_line_by_delivery_move.setdefault(wizard_line.move_id.id, item)
        if unmatched:
            # Every wizard quantity starts at zero, so an unmatched line would
            # otherwise fall through to stock's generic "Please specify at
            # least one non-zero quantity", several frames away and with no
            # clue which RMA line could not be placed.
            raise UserError(_(
                "These RMA lines could not be matched to anything on delivery "
                "%(picking)s, so there is nothing to receive back:\n%(lines)s\n\n"
                "Check that the delivery really shipped these products against "
                "the same sale order lines.",
                picking=picking_id.name,
                lines="\n".join(
                    "- %s" % (line.product_id.display_name or "?")
                    for line in unmatched
                ),
            ))

        new_picking_id = wizard._create_return()
        new_picking_id.write({"picking_type_id": picking_type_id.id, "rma_id": self.id})

        # Link each new return move back to the rma.lines it came from so
        # that, when the warehouse validates the picking with a reduced
        # quantity, _sync_return_to_credit_note can find the right line
        # to update. Phantom-BOM components are deliberately absent from the
        # map: that sync sums quantities per rma.lines, so linking several
        # components to one parent line would inflate the credited amount.
        for move in new_picking_id.move_ids:
            origin = move.origin_returned_move_id
            rma_line = rma_line_by_delivery_move.get(origin.id) if origin else None
            if rma_line:
                move.rma_line_id = rma_line.id

    def _fill_return_wizard_line(self, wizard, rma_line):
        """Put ``rma_line``'s return qty on the wizard lines it belongs to.

        The wizard holds one line per delivery move, all starting at zero;
        only the ones filled in here end up on the return transfer.

        Matching on ``rma_line.product_id`` alone is what broke configured
        products: a Configure Door is delivered as the *manufacturing order's*
        finished good, which is not always the product recorded on the RMA
        line, so nothing matched and every quantity stayed zero. The RMA line
        already knows which products count as its finished good (that is what
        drives the "Return As" column), so reuse that instead of re-deriving
        it, then fall back to components for kits.

        Returns ``(matched, linkable_lines)``: whether anything was filled,
        and the wizard lines that map 1:1 back to this RMA line. Kit
        components are filled but never reported as linkable — several of them
        answer to one RMA line, and _sync_return_to_credit_note sums per line.
        """
        self.ensure_one()
        none_linkable = self.env["stock.return.picking.line"]
        sale_line = rma_line.sale_line_id
        if not sale_line:
            return False, none_linkable
        candidates = wizard.product_return_moves.filtered(
            lambda m: m.move_id.sale_line_id == sale_line
        )
        if not candidates:
            return False, none_linkable

        # 1. Finished good — the delivery shipped the product itself, the CPQ
        #    custom finished product, or the manufacturing order's product.
        finished_products = rma_line._get_return_finished_products()
        finished_lines = candidates.filtered(
            lambda m: m.product_id in finished_products
        )
        if finished_lines:
            if self._spread_return_qty(finished_lines, rma_line.return_qty):
                return True, finished_lines
            return False, none_linkable

        # 2. Components — a phantom/kit BOM ships its parts, not the parent,
        #    so the parent never appears on the delivery.
        bom = rma_line._get_return_bom()
        if bom:
            matched = False
            for bom_line in bom.bom_line_ids:
                component_lines = candidates.filtered(
                    lambda m, b=bom_line: m.product_id == b.product_id
                )
                if component_lines and self._spread_return_qty(
                    component_lines, rma_line.return_qty * bom_line.product_qty
                ):
                    matched = True
            if matched:
                return True, none_linkable

        # 3. Only one move went out for this sale line, so it is unambiguous
        #    even when the products do not line up.
        if len(candidates) == 1 and self._spread_return_qty(
            candidates, rma_line.return_qty
        ):
            return True, candidates
        return False, none_linkable

    @staticmethod
    def _spread_return_qty(wizard_lines, quantity):
        """Spread ``quantity`` over wizard lines, capped by what each shipped.

        A sale line can ship as several moves (partial deliveries, different
        lots); filling only the first would silently under-return.
        """
        remaining = quantity
        filled = False
        for wizard_line in wizard_lines:
            if remaining <= 0:
                break
            capacity = wizard_line.move_quantity or remaining
            take = min(remaining, capacity)
            if take <= 0:
                continue
            wizard_line.quantity = take
            remaining -= take
            filled = True
        # Anything left over (delivery shipped less than the RMA claims) goes
        # on the last line so the requested quantity is never quietly dropped.
        if filled and remaining > 0:
            wizard_lines[-1].quantity += remaining
        return filled

    # DEPRECATED: superseded by _fill_return_wizard_line, which asks the RMA
    # line what its finished good actually is instead of guessing from a
    # phantom-BOM lookup. Kept because it is a public method that server
    # actions or studio automations may still call.
    def has_phantom_bom(self, product, sale_line_id):

        bom_dict = self.env['mrp.bom']._bom_find(
            products = product,
            company_id = sale_line_id.company_id.id,
            bom_type = 'phantom'
        )

        phantom_bom = bom_dict.get(product)

        return phantom_bom

    def _replacement_pricelist_price(self, product, qty):
        """Price a replacement product the way its replacement sale order will."""
        self.ensure_one()
        if not product:
            return 0.0
        pricelist = (
            self.sale_order.pricelist_id
            or self.partner_id.property_product_pricelist
        )
        if pricelist:
            return pricelist._get_product_price(product, qty or 1.0)
        return product.lst_price

    def process_replacement_items(self, replacement_items):
        """This function creates and validates Sale Order for replacement items

        Args:
            replacement_items (_type_): RMA lines to be replaced
        """
        fiscal_position_id = (
            self.env["account.fiscal.position"]
            .with_company(self.sale_order.company_id)
            ._get_fiscal_position(self.sale_order.partner_id)
        )

        # ======================== CREATE SALE ORDER LINES FOR REPLACEMENT ITEMS =========================
        sale_order_lines = []
        for line in replacement_items.filtered(
            lambda t: t.product_id.type != "service"
        ):
            sale_order_line = (
                0,
                0,
                {
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.qty,
                },
            )
            if line.price_unit_manual:
                sale_order_line[2]["price_unit"] = line.price_unit
            sale_order_lines.append(sale_order_line)

        # ======================== CHECK IF A SALE ORDER EXISTS =========================
        sale_order = self.env["sale.order"].search(
            [("origin", "=", self.name), ("state", "in", ["draft", "sent"])]
        )
        if sale_order:
            sale_order.write({"order_line": [(5, 0, 0)] + sale_order_lines})
        else:
            # ======================== CREATE SALE ORDER FOR REPLACEMENT ITEMS =========================
            sale_order = {
                "partner_id": self.partner_id.id,
                "name": self.env["ir.sequence"].next_by_code("sale.order"),
                "origin": self.name,
                "pricelist_id": self.sale_order.pricelist_id.id,
                "team_id": self.sale_order.team_id.id,
                "fiscal_position_id": fiscal_position_id.id
                if fiscal_position_id
                else False,
                "warehouse_id": self.sale_order.warehouse_id.id,
                "date_order": self.date,
                "partner_shipping_id": self.sale_order.partner_shipping_id.id,
                "partner_invoice_id": self.sale_order.partner_invoice_id.id,
                "order_line": sale_order_lines,
            }

            order = self.env["sale.order"].create(sale_order)
            order.order_line._compute_tax_ids()

        self.update({"sale_order_count": 1 + self.sale_order_count})

    def _process_legacy_replacement_items(self, replacement_items):
        self.ensure_one()
        order_line_vals = [
            (0, 0, self._prepare_legacy_replacement_sale_line_vals(line))
            for line in replacement_items.filtered(
                lambda item: item.product_id.type != "service" and item.qty > 0
            )
        ]
        if not order_line_vals:
            return False

        fiscal_position = self.env["account.fiscal.position"].with_company(
            self.company_id
        )._get_fiscal_position(self.partner_id)
        sale_order = self.env["sale.order"].search([
            ("origin", "=", self.name),
            ("rma_id", "=", self.id),
            ("state", "in", ["draft", "sent"]),
        ], limit=1)
        vals = {
            "partner_id": self.partner_id.id,
            "company_id": self.company_id.id,
            "origin": self.name,
            "fiscal_position_id": fiscal_position.id if fiscal_position else False,
            "warehouse_id": self.warehouse_id.id,
            "date_order": self.date,
            "partner_shipping_id": self.partner_id.id,
            "partner_invoice_id": self.partner_id.id,
            "rma_id": self.id,
            "order_line": [(5, 0, 0)] + order_line_vals,
        }
        if self.partner_id.property_product_pricelist:
            vals["pricelist_id"] = self.partner_id.property_product_pricelist.id

        if sale_order:
            sale_order.write(vals)
        else:
            sale_order = self.env["sale.order"].create(vals)
        sale_order.order_line._compute_tax_ids()
        if sale_order.state in ("draft", "sent"):
            sale_order.action_confirm()
        self.update({"sale_order_count": 1 + self.sale_order_count})
        return sale_order

    def _prepare_legacy_replacement_sale_line_vals(self, replacement_line):
        self.ensure_one()
        product = replacement_line.product_id
        discount = 0.0
        vals = {
            "product_id": product.id,
            "product_uom_qty": replacement_line.qty,
            "name": _(
                "Legacy Sage replacement for %(rma)s (%(policy)s)"
            ) % {
                "rma": self.name,
                "policy": dict(self._fields["legacy_replacement_pricing_policy"].selection).get(
                    self.legacy_replacement_pricing_policy
                ),
            },
        }
        if self.legacy_replacement_pricing_policy == "zero_warranty":
            vals["price_unit"] = 0.0
        elif self.legacy_replacement_pricing_policy == "discounted_exchange":
            discount = self.legacy_replacement_discount
        vals["discount"] = discount
        if "unico_purchase_note" in self.env["sale.order.line"]._fields:
            vals["unico_purchase_note"] = self._get_legacy_replacement_purchase_note()
        return vals

    def _get_legacy_replacement_purchase_note(self):
        self.ensure_one()
        policy = dict(self._fields["legacy_replacement_pricing_policy"].selection).get(
            self.legacy_replacement_pricing_policy,
            self.legacy_replacement_pricing_policy,
        )
        source_ref = (
            self.legacy_sage_invoice_ref or self.legacy_sage_order_ref or self.name
        )
        return _(
            "Sage return replacement from %(rma)s. Source reference: %(source)s. "
            "Pricing policy: %(policy)s."
        ) % {
            "rma": self.name,
            "source": source_ref,
            "policy": policy,
        }

    def action_approve(self):
        if self.rma_type in ("rma_with_do", "legacy_sage"):
            if self.state == "approved":
                raise UserError("You cannot approve a RMA more then one time.")

            self.process_rma()
            self.write({"state": "approved"})

    def action_view_receipt(self):
        self.ensure_one()
        return {
            "name": "Picking",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "stock.picking",
            "domain": [("id", "in", self._get_all_rma_pickings().ids)],
        }

    def action_view_refund_invoice(self):
        self.ensure_one()
        return {
            "name": "Credit Note",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "account.move",
            "domain": [("rma_id", "=", self.id)],
        }

    def action_view_sale_order(self):
        return {
            "name": "Sale Order",
            "type": "ir.actions.act_window",
            "view_mode": "list,form",
            "res_model": "sale.order",
            "domain": [("origin", "=", self.name)],
        }

    def action_move_to_draft(self):
        if self.rma_type == "legacy_sage":
            self._release_legacy_entitlements()
        # Clear RMA lines and replacement products
        rma_line_ids = (
            self.env["rma.lines"]
            .search([("rma_id", "=", self.id)])
            .filtered(lambda t: t.rma_resolution_action != False)
        )
        for line in rma_line_ids:
            line.write(
                {
                    "rma_resolution_id": False,
                    "return_reason_id": False,
                    "return_qty": 0,
                }
            )

        replacement_product_ids = self.env["rma.replace.order"].search(
            [("rma_id", "=", self.id)]
        )
        for line in replacement_product_ids:
            line.unlink()

        self.write({"state": "draft"})
        return

    def validate_stock_picking(self):
        pickings = self._get_all_rma_pickings()
        if pickings.filtered(lambda t: t.state not in ("done", "cancel")):
            raise ValidationError(
                "Sorry! You can't proceed until all the stock picking is complete"
            )

    def action_close(self):
        self.validate_stock_picking()
        baseDate = fields.Date.today(self)
        self.write({"state": "close"})
        return

    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            for i in rec.rma_line_ids:
                if (
                    i.product_id.tracking == "serial"
                    and i.return_qty != 0
                    and len(i.lot_ids.ids) > i.return_qty
                ):
                    raise UserError(
                        f"The Product {i.product_id.name} should have count of serial numbers equal/less then the return quantity {i.return_qty}"
                    )
                elif (
                    i.product_id.tracking == "lot"
                    and i.return_qty != 0
                    and len(i.lot_ids.ids) > i.return_qty
                ):
                    raise UserError(
                        f"The Product {i.product_id.name} should have count of lot numbers equal/less then the return quantity {i.return_qty}"
                    )
        return res

    def unlink(self):
        """
        Should allow to delete only if the stage in draft
        """
        for rma in self:
            if rma.state != "draft":
                raise ValidationError(
                    "Sorry! You can't delete the RMA order which is not in draft stage"
                )
        return super().unlink()


class RmaLines(models.Model):
    _name = "rma.lines"
    _description = "Rma Lines"

    rma_id = fields.Many2one("rma.main", "RMA Id")
    rma_state = fields.Selection(related="rma_id.state", store=True, string="State")
    delivery_order = fields.Many2one(
        related="rma_id.delivery_order", store=True, string="Delivery Order"
    )
    sale_order = fields.Many2one(
        related="rma_id.sale_order", store=True, string="Sale Order"
    )
    total_return = fields.Float(
        related="rma_id.total_return", store=True, string="Total Return"
    )
    total_replace = fields.Float(
        related="rma_id.total_replace", store=True, string="Total Replace"
    )
    total_difference = fields.Float(
        related="rma_id.total_difference", store=True, string="Total Difference"
    )
    rma_resolution_id = fields.Many2one("rma.resolution", "Action")
    return_reason_id = fields.Many2one("return.reason", "Return Reason")
    rma_resolution_action = fields.Selection(
        related="rma_resolution_id.rma_action", store=True, string="Resolution Action"
    )
    date = fields.Datetime(related="rma_id.date", store=True)
    state = fields.Selection(related="rma_id.state")
    product_id = fields.Many2one("product.product", "Product")
    original_description = fields.Text(
        "Original Product Description",
        compute="_compute_return_context",
        compute_sudo=True,
    )
    return_scope = fields.Selection(
        [
            ("finished_good", "Finished Good"),
            ("components", "Components"),
        ],
        string="Return Scope",
        compute="_compute_return_context",
        compute_sudo=True,
    )
    return_scope_label = fields.Char(
        "Return As",
        compute="_compute_return_context",
        compute_sudo=True,
    )
    return_component_summary = fields.Text(
        "Component Details",
        compute="_compute_return_context",
        compute_sudo=True,
    )
    detailed_type = fields.Selection(related="product_id.type")
    delivery_qty = fields.Float("Delivered Quantity")
    return_qty = fields.Float("Return Quantity")
    received_qty = fields.Float("Received Quantity")
    price_unit = fields.Float("Price")
    total_price = fields.Float("Total Price", compute="_update_total_price")
    legacy_entitlement_id = fields.Many2one(
        "rma.legacy.entitlement",
        string="Original Sage Sale Line",
        copy=False,
    )
    legacy_entitlement_available_qty = fields.Float(
        "Qty Left to Return",
        compute="_compute_legacy_entitlement_available_qty",
    )
    legacy_entitlement_consumed_qty = fields.Float(
        "Qty Used by This RMA",
        copy=False,
        readonly=True,
    )
    legacy_max_return_qty = fields.Float(
        "Max Return Quantity",
        help="Maximum quantity the customer can return from the original Sage sale.",
    )
    legacy_unit_cost = fields.Float("Original Unit Cost")
    legacy_refund_account_id = fields.Many2one(
        "account.account",
        string="Credit Account",
        domain=[
            (
                "account_type",
                "not in",
                ("asset_receivable", "liability_payable", "asset_cash", "liability_credit_card"),
            )
        ],
    )
    legacy_tax_ids = fields.Many2many(
        "account.tax",
        "rma_line_legacy_tax_rel",
        "rma_line_id",
        "tax_id",
        string="Taxes to Use on Credit",
        domain=[("type_tax_use", "=", "sale")],
    )
    replaced_with = fields.Many2many(
        "product.product", "rma_lin_prds_id", string="Replaced with"
    )
    replaced_qty = fields.Float("Replaced Quantity")
    is_invoice = fields.Boolean("Is invoice", default=False)
    lot_ids = fields.Many2many(
        "stock.lot", "rel_rma_product_id", string="Lot/Serial Numbers"
    )
    sale_line_id = fields.Many2one(
        string="Order Line", comodel_name="sale.order.line", ondelete="restrict"
    )
    move_line_id = fields.Many2one(
        string="Move Order Line", comodel_name="stock.move.line", ondelete="set null"
    )

    stock_move_id = fields.Many2one(
        string="Stock Move", comodel_name="stock.move", ondelete="restrict"
    )
    pending_qty = fields.Float(string="Pending Qty")
    rma_line_stock_move_ids = fields.One2many(
        "stock.move",
        "rma_line_id",
        string="To get all the stock move for this sale line id stock",
    )
    rma_line_account_move_line_ids = fields.One2many(
        "account.move.line",
        "rma_line_id",
        string="To get all the stock move for this sale line id Account",
    )
    rma_inward_qty = fields.Float(
        compute="compute_rma_inward_credit_note_qty", string="RMA Stock Inward Qty"
    )
    rma_credit_note_qty = fields.Float(
        compute="compute_rma_inward_credit_note_qty", string="RMA Credit Note Qty"
    )

    def _get_return_context_dependencies(self):
        dependencies = [
            "product_id",
            "sale_line_id",
            "sale_line_id.name",
            "delivery_qty",
            "return_qty",
            "rma_id.delivery_order",
        ]
        if "cpq_configuration_description" in self.env["sale.order.line"]._fields:
            dependencies.append("sale_line_id.cpq_configuration_description")
        return dependencies

    @api.depends(lambda self: self._get_return_context_dependencies())
    def _compute_return_context(self):
        for line in self:
            line.original_description = line._get_original_product_description()
            line.return_scope = False
            line.return_scope_label = False
            line.return_component_summary = False

            if not line.product_id or not line.sale_line_id:
                continue

            delivery_moves = line._get_original_delivery_moves()
            bom = line._get_return_bom()
            return_scope = line._get_return_scope(delivery_moves, bom)
            basis_qty = line.return_qty if line.return_qty > 0 else line.delivery_qty
            components = line._get_return_component_values(
                return_scope,
                delivery_moves,
                bom,
                basis_qty,
            )

            line.return_scope = return_scope
            line.return_scope_label = (
                _("Components")
                if return_scope == "components"
                else _("Finished Good")
            )
            line.return_component_summary = "\n".join(
                line._format_return_component(component)
                for component in components
            ) or False

    def _get_original_product_description(self):
        self.ensure_one()
        sale_line = self.sale_line_id
        if not sale_line:
            return self.product_id.display_name or False

        description_parts = []
        candidates = [sale_line.name]
        if "cpq_configuration_description" in sale_line._fields:
            candidates.append(sale_line.cpq_configuration_description)

        seen = set()
        for candidate in candidates:
            text = (candidate or "").strip()
            normalized = " ".join(text.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            description_parts.append(text)

        return "\n".join(description_parts) or self.product_id.display_name or False

    def _get_original_delivery_moves(self):
        self.ensure_one()
        if not self.rma_id.delivery_order or not self.sale_line_id:
            return self.env["stock.move"]
        return self.rma_id.delivery_order.move_ids.filtered(
            lambda move: (
                move.state != "cancel"
                and move.sale_line_id == self.sale_line_id
                and move.product_id
            )
        )

    def _get_return_bom(self):
        self.ensure_one()
        sale_line = self.sale_line_id
        if not sale_line or not self.product_id:
            return self.env["mrp.bom"]

        if "cpq_bom_id" in sale_line._fields and sale_line.cpq_bom_id:
            return sale_line.cpq_bom_id

        if "cpq_production_ids" in sale_line._fields:
            productions = sale_line.cpq_production_ids.filtered(
                lambda production: production.state != "cancel" and production.bom_id
            )
            production = productions.filtered(
                lambda production: production.state == "done"
            )[:1]
            production = production or productions[:1]
            if production:
                return production.bom_id

        if (
            "cpq_custom_finished_bom_id" in sale_line._fields
            and sale_line.cpq_custom_finished_bom_id
        ):
            return sale_line.cpq_custom_finished_bom_id

        bom_by_product = self.env["mrp.bom"]._bom_find(
            products=self.product_id,
            company_id=(sale_line.company_id or self.env.company).id,
        )
        return bom_by_product.get(self.product_id, self.env["mrp.bom"])

    def _get_return_scope(self, delivery_moves, bom):
        self.ensure_one()
        finished_products = self._get_return_finished_products()
        if delivery_moves.filtered(lambda move: move.product_id in finished_products):
            return "finished_good"
        if delivery_moves:
            return "components"
        if bom and bom.type == "phantom":
            return "components"
        return "finished_good"

    def _get_return_finished_products(self):
        self.ensure_one()
        finished_products = self.product_id
        sale_line = self.sale_line_id
        if not sale_line:
            return finished_products

        if (
            "cpq_custom_finished_product_id" in sale_line._fields
            and sale_line.cpq_custom_finished_product_id
        ):
            finished_products |= sale_line.cpq_custom_finished_product_id
        if "cpq_production_ids" in sale_line._fields:
            finished_products |= sale_line.cpq_production_ids.filtered(
                lambda production: production.state != "cancel"
            ).mapped("product_id")
        return finished_products

    def _get_return_component_values(self, return_scope, delivery_moves, bom, basis_qty):
        self.ensure_one()
        if basis_qty <= 0:
            return []
        if return_scope == "components" and delivery_moves:
            return self._get_move_component_values(delivery_moves, basis_qty)
        if bom:
            return self._get_bom_component_values(bom, basis_qty)
        return []

    def _get_move_component_values(self, delivery_moves, basis_qty):
        self.ensure_one()
        delivery_qty = self.delivery_qty or basis_qty
        factor = basis_qty / delivery_qty if delivery_qty else 0.0
        return self._aggregate_return_components(
            {
                "product": move.product_id,
                "quantity": (
                    move.quantity if move.state == "done" else move.product_uom_qty
                ) * factor,
                "uom": move.product_uom or move.product_id.uom_id,
            }
            for move in delivery_moves
        )

    def _get_bom_component_values(self, bom, basis_qty):
        self.ensure_one()
        parent_qty = basis_qty
        sale_uom = self.sale_line_id.product_uom_id or self.product_id.uom_id
        if (
            sale_uom
            and bom.product_uom_id
            and sale_uom._has_common_reference(bom.product_uom_id)
            and sale_uom != bom.product_uom_id
        ):
            parent_qty = sale_uom._compute_quantity(
                parent_qty,
                bom.product_uom_id,
                round=False,
            )

        factor = parent_qty / (bom.product_qty or 1.0)
        bom_product = bom.product_id or self.product_id
        bom_lines = bom.bom_line_ids.filtered(
            lambda bom_line: (
                bom_line.product_id
                and not bom_line._skip_bom_line(bom_product)
            )
        )
        return self._aggregate_return_components(
            {
                "product": bom_line.product_id,
                "quantity": (bom_line.product_qty or 0.0) * factor,
                "uom": bom_line.product_uom_id or bom_line.product_id.uom_id,
            }
            for bom_line in bom_lines
        )

    def _aggregate_return_components(self, components):
        grouped = {}
        for component in components:
            product = component.get("product")
            if not product:
                continue
            uom = component.get("uom") or product.uom_id
            key = (product.id, uom.id if uom else False)
            if key not in grouped:
                grouped[key] = {
                    "product": product,
                    "quantity": 0.0,
                    "uom": uom,
                }
            grouped[key]["quantity"] += component.get("quantity") or 0.0
        return sorted(
            grouped.values(),
            key=lambda component: component["product"].display_name,
        )

    def _format_return_component(self, component):
        quantity_text = formatLang(
            self.env,
            component["quantity"] or 0.0,
            dp=RMA_PRODUCT_UOM_DECIMAL_PRECISION,
        )
        uom_name = component["uom"].name if component.get("uom") else ""
        product_name = component["product"].display_name
        if uom_name:
            return "%s %s - %s" % (quantity_text, uom_name, product_name)
        return "%s - %s" % (quantity_text, product_name)

    def compute_rma_inward_credit_note_qty(self):
        for rma_line in self:
            if rma_line.rma_line_stock_move_ids:
                rma_line.rma_inward_qty = sum(
                    rma_line.rma_line_stock_move_ids.filtered(
                        lambda stockmove: stockmove.state == "done"
                    ).mapped("quantity")
                )
            else:
                rma_line.rma_inward_qty = 0.00
            if rma_line.rma_line_account_move_line_ids:
                rma_line.rma_credit_note_qty = sum(
                    rma_line.rma_line_account_move_line_ids.sudo()
                    .filtered(
                        lambda accountmoveline: accountmoveline.parent_state == "posted"
                    )
                    .mapped("quantity")
                )
            else:
                rma_line.rma_credit_note_qty = 0.00

    @api.onchange(
        "return_qty",
        "price_unit",
    )
    @api.depends(
        "return_qty",
        "price_unit",
    )
    def _update_total_price(self):
        for line in self:
            line.total_price = line.return_qty * line.price_unit

    @api.depends(
        "legacy_entitlement_id.qty_available",
        "legacy_entitlement_consumed_qty",
    )
    def _compute_legacy_entitlement_available_qty(self):
        for line in self:
            line.legacy_entitlement_available_qty = line._get_legacy_entitlement_available_qty()

    def _get_legacy_entitlement_available_qty(self):
        self.ensure_one()
        if not self.legacy_entitlement_id:
            return 0.0
        return (
            self.legacy_entitlement_id.qty_available
            + self.legacy_entitlement_consumed_qty
        )

    def _sync_legacy_entitlement_consumed_qty(self, received_qty):
        self.ensure_one()
        if not self.legacy_entitlement_id:
            return
        delta = received_qty - self.legacy_entitlement_consumed_qty
        if delta > 0:
            self.legacy_entitlement_id.sudo().consume_return_qty(delta)
        elif delta < 0:
            self.legacy_entitlement_id.sudo().release_return_qty(abs(delta))
        self.legacy_entitlement_consumed_qty = received_qty

    def _get_legacy_credit_line_name(self):
        self.ensure_one()
        parts = [self.product_id.display_name]
        if self.rma_id.legacy_sage_invoice_ref:
            parts.append(_("Sage invoice %s") % self.rma_id.legacy_sage_invoice_ref)
        elif self.rma_id.legacy_sage_order_ref:
            parts.append(_("Sage order %s") % self.rma_id.legacy_sage_order_ref)
        return " - ".join(parts)

    @api.onchange("return_qty")
    def _onchange_return_qty(self):
        if self.return_qty and self.rma_id.rma_type != "legacy_sage":
            if self.delivery_qty < self.return_qty:
                raise UserError(_("Quantity should be less than delivered."))

    @api.onchange("legacy_entitlement_id")
    def _onchange_legacy_entitlement_id(self):
        self._apply_legacy_entitlement_defaults()

    def _apply_legacy_entitlement_defaults(self):
        for line in self:
            entitlement = line.legacy_entitlement_id
            if not entitlement:
                continue
            line.product_id = entitlement.product_id
            line.price_unit = entitlement.price_unit
            line.legacy_max_return_qty = line._get_legacy_entitlement_available_qty()
            line.legacy_unit_cost = entitlement.unit_cost
            line.legacy_refund_account_id = entitlement.refund_account_id
            line.legacy_tax_ids = entitlement.tax_ids
            if entitlement.lot_id and not line.lot_ids:
                line.lot_ids = [(6, 0, entitlement.lot_id.ids)]


class RmaReplaceOrder(models.Model):
    _name = "rma.replace.order"
    _description = "Rma Replace Order"

    product_id = fields.Many2one("product.product", string="Product")
    product_detailed_type = fields.Selection(related="product_id.type")
    qty = fields.Float("qty", default=0.0, digits="Product Unit")
    rma_id = fields.Many2one("rma.main", string="RMA Order")
    price_unit_manual = fields.Boolean("Manual Price", default=False)
    manual_price_unit = fields.Float("Manual Unit Price", default=0.0, digits="Product Price")
    price_unit = fields.Float(
        "Unit Price",
        compute="_compute_price_unit",
        inverse="_inverse_price_unit",
        store=True,
        readonly=False,
        digits="Product Price",
    )
    total_price = fields.Float(
        "Total Price",
        default=0.0,
        compute="_update_total_price",
        store=True,
    )

    def write(self, vals):
        # A product swap invalidates a previously captured manual price unless
        # the caller manages the override fields explicitly.
        if (
            "product_id" in vals
            and "price_unit_manual" not in vals
            and "manual_price_unit" not in vals
        ):
            vals = dict(vals, price_unit_manual=False, manual_price_unit=0.0)
        return super().write(vals)

    @api.constrains("product_id", "qty")
    def _check_serial_replacement_qty(self):
        for line in self:
            if line.product_id.tracking == "serial" and line.qty and not line.qty.is_integer():
                raise ValidationError(_("Serial-numbered replacement products must use whole-number quantities."))

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for line in self:
            line.price_unit_manual = False
            line.manual_price_unit = 0.0

    @api.onchange("price_unit_manual")
    def _onchange_price_unit_manual(self):
        # Seed the override with the displayed price so opting into manual
        # pricing does not blank the field.
        for line in self:
            if line.price_unit_manual:
                line.manual_price_unit = line.price_unit

    @api.depends(
        "product_id",
        "product_id.lst_price",
        "qty",
        "price_unit_manual",
        "manual_price_unit",
        "rma_id.sale_order.pricelist_id",
    )
    def _compute_price_unit(self):
        for line in self:
            if line.price_unit_manual:
                line.price_unit = line.manual_price_unit
            elif line.rma_id:
                line.price_unit = line.rma_id._replacement_pricelist_price(
                    line.product_id, line.qty
                )
            else:
                line.price_unit = line.product_id.lst_price if line.product_id else 0.0

    def _inverse_price_unit(self):
        for line in self:
            if line.price_unit_manual:
                line.manual_price_unit = line.price_unit

    @api.depends("qty", "price_unit")
    def _update_total_price(self):
        for line in self:
            line.total_price = line.price_unit * line.qty


class RejectWizard(models.Model):
    _name = "return.reason"
    _description = "Return Reason"
    _rec_name = "name"

    name = fields.Char("Return Reason")
    is_customer_return_reason = fields.Boolean(
        string="Is Customer Return Reason", default=True
    )
    active = fields.Boolean(default=True)


class RmaClaim(models.Model):
    _name = "rma.claim"
    _description = "Rma Claim"
    _rec_name = "rma_id"

    rma_id = fields.Many2one("rma.main", "RMA Number")
    subject = fields.Char("Subject")
    partner = fields.Many2one("res.partner", "Partner", store=True)
    responsible = fields.Many2one("res.users", "Responsible", store=True)
    date = fields.Datetime("Date")
    nxt_act_dt = fields.Datetime("Next Action Date")
    nxt_act = fields.Char("Next Action")
    stock_picking_id = fields.Many2one("stock.picking")


class RmaResolution(models.Model):
    _name = "rma.resolution"
    _description = "Rma Resolution"
    _rec_name = "rma_resolution"

    rma_resolution = fields.Char("Resolution", required=True)
    rma_action = fields.Selection(
        [
            ("refund", "Refund"),
            ("refund_with_returned_item", "Refund With Returned Items"),
            ("replacement", "Replacement"),
            ("replacement_with_returned_item", "Replacement With Returned Items"),
            ("return_with_credit", "Return item with credit"),
        ],
        string="Action",
    )
    apply_return_fee = fields.Boolean(
        "Apply Return Fee",
        help="If set, a Return Fee line is appended to the credit note when this "
             "resolution is used on refund/refund-with-return items.",
    )
    return_fee_type = fields.Selection(
        [("amount", "Fixed Amount"), ("percentage", "Percentage of Refund")],
        string="Fee Type",
        default="amount",
    )
    return_fee_value = fields.Float(
        "Fee Value",
        help="Fixed amount, or percentage of the refunded subtotal (e.g. 10 = 10%).",
    )
    active = fields.Boolean(default=True)
    rma_main_ids = fields.One2many("rma.main", "rma_resolution_id")
    count_rma = fields.Integer(compute="get_len_rma_main_ids")

    def get_len_rma_main_ids(self):
        for reason in self:
            reason.count_rma = len(reason.rma_main_ids)

    def unlink(self):
        for reason in self:
            if reason.count_rma:
                raise ValidationError(
                    "Sorry! this reason was used in the RMA so you can't delete use archive"
                )
        return super().unlink()
