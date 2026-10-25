from urllib.parse import quote

from markupsafe import Markup

from odoo import _, api, fields, models


class StockPicking(models.Model):
    _inherit = "stock.picking"

    # ------------------------------------------------------------------
    # Backorder -> notify the sale order's salesperson
    # ------------------------------------------------------------------
    def _create_backorder(self, backorder_moves=None):
        """Alert the salesperson when a backorder is created for their order.

        ``_create_backorder`` runs when a user chooses to create a backorder on a
        delivery (and for the always-create-backorder operation policy). We email
        the linked sale order's salesperson with the short-shipped items so they
        can proactively inform the customer, closing the communication gap.
        """
        backorders = super()._create_backorder(backorder_moves=backorder_moves)
        backorders.sudo()._unico_notify_backorder_salesperson()
        return backorders

    def _send_confirmation_email(self):
        """Skip the delivery confirmation mail when customer emails are off.

        Core already gates this on ``company.stock_move_email_validation``;
        this makes the Unico switch authoritative too, so all four automatic
        customer mails answer to one setting. The salesperson backorder alert
        below is deliberately unaffected — it goes to staff, not customers.
        """
        allowed = self.filtered(
            lambda picking: picking.company_id.unico_send_automatic_customer_emails
        )
        return super(StockPicking, allowed)._send_confirmation_email()

    def _unico_notify_backorder_salesperson(self):
        for backorder in self:
            mail_values, note_body, subject = backorder._unico_backorder_notification()
            if not mail_values:
                continue
            # Guaranteed email to the salesperson: a mail.mail is sent to their
            # address directly, so it does NOT depend on their in-app/email
            # notification preference, and the customer is never a recipient.
            self.env["mail.mail"].sudo().create(mail_values).send()
            # Internal audit note on the order (notifies nobody — the email is
            # the alert; this is just a visible trail for the sales team).
            backorder.sale_id.message_post(
                body=note_body, subject=subject, subtype_xmlid="mail.mt_note",
            )

    def _unico_backorder_notification(self):
        """Build the salesperson backorder alert for this (backorder) picking.

        Returns ``(mail_values, note_body, subject)`` for the guaranteed email to
        the sale order's salesperson and the internal audit note, or
        ``(None, None, None)`` when no alert applies (non-customer delivery, no
        linked order, or no salesperson with an email).
        """
        self.ensure_one()
        order = self.sale_id
        salesperson = order.user_id if order else False
        if (
            self.picking_type_id.code != "outgoing"
            or not order
            or not salesperson
            or not salesperson.partner_id.email_formatted
        ):
            return None, None, None

        items = Markup().join(
            Markup("<li>%s — %s %s</li>") % (
                move.product_id.display_name,
                "%g" % move.product_uom_qty,
                move.product_uom.name or "",
            )
            for move in self.move_ids
            if move.product_id
        )
        items_block = Markup("<ul>%s</ul>") % items if items else Markup("")

        def _intro(backorder_ref, order_ref):
            return Markup(_(
                "A backorder %(backorder)s was created for order %(order)s. The "
                "following item(s) are short-shipped and still pending — please "
                "follow up with the customer about the delay:"
            )) % {"backorder": backorder_ref, "order": order_ref}

        subject = _(
            "Backorder %(backorder)s created for %(order)s",
            backorder=self.name, order=order.name,
        )
        mail_values = {
            "subject": subject,
            "body_html": _intro(self.name, order.name) + items_block,
            "email_from": self.env.company.partner_id.email_formatted or self.env.user.email_formatted,
            "email_to": salesperson.partner_id.email_formatted,
            "model": "sale.order",
            "res_id": order.id,
            # Keep the record so the sent alert is auditable (Technical > Emails)
            # instead of vanishing right after sending.
            "auto_delete": False,
        }
        recipient_line = Markup("<p><em>%s</em></p>") % _(
            "This alert was emailed to the salesperson %(name)s (%(email)s).",
            name=salesperson.name, email=salesperson.partner_id.email,
        )
        note_body = (
            _intro(self._get_html_link(), order._get_html_link())
            + items_block
            + recipient_line
        )
        return mail_values, note_body, subject

    fulfillment_type = fields.Selection(
        related="sale_id.fulfillment_type",
        string="Fulfillment Type",
        store=True,
        index=True,
    )
    unico_delivery_address_display = fields.Char(
        string="Real Address",
        compute="_compute_unico_delivery_address_display",
        help="Real delivery / pickup address for the operations list. Uses the "
             "structured shipping address when a sale order is linked, otherwise "
             "the picking customer address.",
    )
    unico_report_print_count = fields.Integer(
        string="Report Print Count",
        default=0,
        copy=False,
        help="How many times the SPF / picking PDF has been printed for this "
             "transfer. The first print is the clean original; every reprint adds "
             "a diagonal 'Duplicate Copy N' watermark (copy 2 = Duplicate Copy 1).",
    )
    unico_original_print_date = fields.Datetime(
        string="Original Printed On",
        readonly=True,
        copy=False,
        help="Date and time the SPF / picking PDF was first printed for this "
             "transfer. Set once on the first print and never changed by reprints.",
    )
    unico_last_print_date = fields.Datetime(
        string="Last Printed On",
        readonly=True,
        copy=False,
        help="Date and time the SPF / picking PDF was most recently printed for "
             "this transfer. Updated on every print / reprint.",
    )
    unico_is_spf = fields.Boolean(
        string="SPF/SFP Picking",
        compute="_compute_unico_is_spf",
        store=True,
        help="True when the transfer reference contains 'SPF' or 'SFP'. Drives "
             "the green 'Printed' reprint ribbon on the transfer form.",
    )

    @api.depends("name")
    def _compute_unico_is_spf(self):
        for picking in self:
            name = (picking.name or "").upper()
            picking.unico_is_spf = "SPF" in name or "SFP" in name

    @api.depends(
        "sale_id",
        "partner_id",
        "sale_id.partner_shipping_id",
    )
    def _compute_unico_delivery_address_display(self):
        for picking in self:
            picking.unico_delivery_address_display = (
                picking._get_unico_delivery_address_display()
            )

    def _get_unico_delivery_address_display(self):
        self.ensure_one()
        order = self.sale_id
        partner = (order.partner_shipping_id if order else False) or self.partner_id
        if not partner:
            return False
        address = partner._display_address(without_company=True) or ""
        return ", ".join(part.strip() for part in address.splitlines() if part.strip())

    def _unico_duplicate_watermark_style(self):
        """Inline CSS for the diagonal "Duplicate Copy N" reprint watermark.

        Rendered as a *repeating* SVG background on the report ``.page`` element:
        wkhtmltopdf paints a repeating background on every physical page box, which
        is the only place-it-on-every-page mechanism that survives here — a fixed
        body element shows on page 1 only, and footer/header overflow is clipped to
        its strip. Returns an empty string for the clean first copy so the original
        print is unmarked.
        """
        self.ensure_one()
        copy_no = (self.unico_report_print_count or 0) - 1
        if copy_no < 1:
            return ""
        # The watermark is a background on the report .page element, but that box is
        # only as tall as its CONTENT — on a short slip the lower part is cut off. So
        # we force the page to ~one printable page tall (min-height: 190mm, safe for
        # both A4 ~213mm content and US Letter without spilling to a blank page).
        #
        # The tile height is set by the SVG's own dimensions (~one page), NOT by
        # background-size: wkhtmltopdf's old WebKit drops a background whose
        # background-size uses mm units. The label is centred in the tile so it lands
        # near page centre; repeat-y still covers every page of a multi-page slip; a
        # wide tile avoids clipping the rotation.
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='760' height='700'>"
            "<text x='380' y='350' fill='#8a8a8a' fill-opacity='0.45' "
            "font-family='Helvetica, Arial, sans-serif' font-size='46' "
            "font-weight='bold' letter-spacing='4' text-anchor='middle' "
            "transform='rotate(-45 380 350)'>Duplicate Copy %s</text></svg>"
        ) % copy_no
        return (
            'background-image: url("data:image/svg+xml,%s"); '
            "background-repeat: repeat-y; background-position: center top; "
            "min-height: 190mm;"
            % quote(svg)
        )


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    count_picking_delivery = fields.Integer(compute="_compute_fulfillment_count")
    count_picking_pickup = fields.Integer(compute="_compute_fulfillment_count")

    def _compute_fulfillment_count(self):
        domains = {
            "count_picking_delivery": [("fulfillment_type", "=", "delivery")],
            "count_picking_pickup": [("fulfillment_type", "=", "will_call")],
        }
        for field_name, domain in domains.items():
            data = self.env["stock.picking"]._read_group(
                domain
                + [
                    ("state", "not in", ("done", "cancel")),
                    ("picking_type_id", "in", self.ids),
                ],
                ["picking_type_id"],
                ["__count"],
            )
            count = {picking_type.id: cnt for picking_type, cnt in data}
            for record in self:
                record[field_name] = count.get(record.id, 0)

    def _get_action_picking_tree_fulfillment(self, fulfillment_type):
        action = self.env["ir.actions.actions"]._for_xml_id(
            "stock.stock_picking_action_picking_type"
        )
        action["domain"] = [
            ("picking_type_id", "=", self.id),
            ("fulfillment_type", "=", fulfillment_type),
            ("state", "not in", ("done", "cancel")),
        ]
        action["context"] = {"default_picking_type_id": self.id}
        return action

    def get_action_picking_tree_delivery(self):
        return self._get_action_picking_tree_fulfillment("delivery")

    def get_action_picking_tree_pickup(self):
        return self._get_action_picking_tree_fulfillment("will_call")
