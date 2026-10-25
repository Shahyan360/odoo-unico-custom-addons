import re
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

# Open purchases that the urgency dashboard should rank/track.
OPEN_PO_STATES = ("draft", "sent", "to approve", "purchase")

# Origin is free text that may merge several source documents ("S001, S002")
# and our auto-RFQ flows append a line suffix ("S001 / 3"); split on these to
# recover the bare source-document tokens.
_ORIGIN_SPLIT = re.compile(r"[,;]")

# Sale states whose commitment date should drive purchase urgency. Quotations
# (draft/sent) are included so urgency reflects an upcoming promise even before
# the sale is confirmed; cancelled/locked sales are excluded.
OPEN_SALE_STATES = ("draft", "sent", "sale")

# Sale commitment within this many days (and not past) counts as "urgent".
URGENT_WINDOW_DAYS = 3


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    # Odoo defaults the Purchase Representative (user_id) to whoever creates the
    # order. Businesses often want every RFQ/PO — whether raised manually or by
    # our sale-confirmation / reordering automation — routed to one dedicated
    # buyer instead. Override the field default (not create) so it still applies
    # to programmatic creates via _add_missing_default_values, yet an explicit
    # choice in the form is left untouched.
    user_id = fields.Many2one(default=lambda self: self._unico_default_purchase_user())

    @api.model
    def _unico_default_purchase_user(self):
        company = self.env.company
        return company.unico_default_purchase_user_id or self.env.user

    unico_earliest_commitment = fields.Datetime(
        string="Earliest Sale Commitment",
        compute="_compute_unico_urgency",
        store=True,
        help="Soonest commitment date among open sales/quotations that need a "
             "product on this purchase (matched by product). Drives the urgency "
             "level. Kept current by sale.order writes and a daily cron.",
    )
    unico_urgency = fields.Selection(
        selection=[
            ("overdue", "Overdue"),
            ("urgent", "Urgent"),
            ("normal", "Normal"),
            ("none", "No Linked Sale"),
        ],
        string="Sales Urgency",
        compute="_compute_unico_urgency",
        store=True,
        index=True,
        default="none",
        help="How time-critical this purchase is for the sales pipeline, from the "
             "earliest linked sale's commitment date: overdue, due within "
             "%s days (urgent), later (normal), or no linked sale." % URGENT_WINDOW_DAYS,
    )
    unico_sale_order_status = fields.Char(
        string="Sale Order Status",
        compute="_compute_unico_sale_order_status",
        search="_search_unico_sale_order_status",
        help="Status of the sales order(s) this RFQ was sourced from (matched by "
             "the sale's reference on the purchase origin), so the buyer can tell "
             "whether the demand is still a quotation, a confirmed order, or "
             "cancelled before confirming the RFQ. 'N/A' when no sale is linked.",
    )
    unico_has_linked_sale = fields.Boolean(
        string="Linked to a Sales Order",
        compute="_compute_unico_has_linked_sale",
        store=True,
        index=True,
        help="Whether this purchase was raised for a sales order (its origin "
             "names a real sale), as opposed to Min/Max replenishment. Stored "
             "so the buyer can filter the RFQ list down to customer-driven "
             "demand; the status label itself is unico_sale_order_status.",
    )
    unico_po_group_key = fields.Char(
        string="Procurement Group Key",
        copy=False,
        index=True,
        help="Internal key that keeps auto-generated reordering RFQs separated per "
             "source: one draft PO per sale order, or one per reordering rule (OP). "
             "Reorders feeding the same sale order share a key; unrelated reorders "
             "do not. Use Merge POs to combine them manually.",
    )

    # ------------------------------------------------------------------
    # Sourcing sale order(s) behind this RFQ (origin-based, like sale.order's
    # _unico_related_purchase_orders in the opposite direction).
    # ------------------------------------------------------------------
    def _unico_origin_sale_tokens(self):
        """Bare sale-order names referenced by this purchase's ``origin``."""
        self.ensure_one()
        tokens = set()
        for part in _ORIGIN_SPLIT.split(self.origin or ""):
            token = part.split("/")[0].strip()
            if token:
                tokens.add(token)
        return tokens

    def _unico_matched_sale_orders(self):
        """``{po.id: [sale.order, ...]}`` for the sales each origin references.

        Resolves every candidate sale name across the whole recordset in one
        search, then maps the matches back per purchase, so both the status
        label and the linked-sale flag cost a single query for the batch.
        """
        tokens_by_po = {po.id: po._unico_origin_sale_tokens() for po in self}
        all_tokens = set().union(*tokens_by_po.values()) if tokens_by_po else set()
        SaleOrder = self.env["sale.order"].sudo()
        sales = SaleOrder.search([("name", "in", list(all_tokens))]) if all_tokens else SaleOrder
        sales_by_name = {s.name: s for s in sales}
        return {
            po.id: [
                sales_by_name[token]
                for token in tokens_by_po[po.id]
                if token in sales_by_name
                and sales_by_name[token].company_id == po.company_id
            ]
            for po in self
        }

    @api.depends("origin")
    def _compute_unico_has_linked_sale(self):
        matched_by_po = self._unico_matched_sale_orders()
        for po in self:
            po.unico_has_linked_sale = bool(matched_by_po[po.id])

    @api.depends("origin")
    def _compute_unico_sale_order_status(self):
        matched_by_po = self._unico_matched_sale_orders()
        state_labels = dict(
            self.env["sale.order"]._fields["state"]._description_selection(self.env)
        )
        for po in self:
            matched = matched_by_po[po.id]
            if not matched:
                po.unico_sale_order_status = "N/A"
            elif len(matched) > 1:
                po.unico_sale_order_status = _("Multiple Sales Orders")
            else:
                po.unico_sale_order_status = state_labels.get(
                    matched[0].state, matched[0].state
                )

    def _search_unico_sale_order_status(self, operator, value):
        """Let the search view filter on the status the buyer sees.

        The label is derived from the free-text ``origin``, so there is no
        column to filter on — resolve the link here and hand back an id
        domain instead. ``value`` is one or more sale.order states
        ('sale', 'draft', 'sent', 'cancel'); a purchase matches when *any* of
        its linked sales is in one of them, so an RFQ covering several sales
        still counts as ready to order as long as one of them is confirmed.

        Nothing is stored, so this always reflects the sale as it stands right
        now: confirming a quotation makes its RFQ appear immediately.
        """
        if operator not in ("=", "!=", "in", "not in"):
            raise UserError(_(
                "Sale Order Status can only be filtered by equality, not '%s'.",
                operator,
            ))
        wanted = {value} if isinstance(value, str) else set(value or ())
        # Only purchases that name a source document can match a sale at all;
        # the rest are Min/Max replenishment (filter those with the stored
        # unico_has_linked_sale flag instead).
        candidates = self.search([("origin", "!=", False)])
        matched_by_po = candidates._unico_matched_sale_orders()
        ids = [
            po.id for po in candidates
            if {sale.state for sale in matched_by_po[po.id]} & wanted
        ]
        return [("id", "not in" if operator in ("!=", "not in") else "in", ids)]

    # NOTE: this stored field also depends on the *linked sales'* commitment_date
    # and state, which Odoo cannot express as a depends path (the link is
    # product-based, not a stored relation). sale.order.write/create therefore
    # call _unico_recompute_urgency() on the affected purchases, and a daily cron
    # re-buckets everything for the time-relative thresholds.
    @api.depends("order_line.product_id", "state")
    def _compute_unico_urgency(self):
        today = fields.Date.context_today(self)
        for po in self:
            commitment = False
            products = po.order_line.product_id
            if products and po.state in OPEN_PO_STATES:
                sale_lines = self.env["sale.order.line"].search([
                    ("product_id", "in", products.ids),
                    ("order_id.state", "in", OPEN_SALE_STATES),
                    ("order_id.company_id", "=", po.company_id.id),
                ])
                dates = [d for d in sale_lines.order_id.mapped("commitment_date") if d]
                commitment = min(dates) if dates else False
            po.unico_earliest_commitment = commitment
            if not commitment:
                po.unico_urgency = "none"
            else:
                days = (commitment.date() - today).days
                if days < 0:
                    po.unico_urgency = "overdue"
                elif days <= URGENT_WINDOW_DAYS:
                    po.unico_urgency = "urgent"
                else:
                    po.unico_urgency = "normal"

    def _unico_recompute_urgency(self):
        """Force a recompute + store of the urgency fields on this recordset.

        Used whenever something the standard ``@api.depends`` cannot see has
        changed — a linked sale's commitment date/state (via sale.order), or the
        passage of time (via the daily cron)."""
        if not self:
            return
        fields_to_refresh = ["unico_earliest_commitment", "unico_urgency"]
        self.invalidate_recordset(fields_to_refresh)
        for fname in fields_to_refresh:
            self.env.add_to_compute(self._fields[fname], self)
        self.flush_recordset(fields_to_refresh)

    @api.model
    def _cron_recompute_unico_urgency(self):
        """Re-bucket open purchases daily — urgency is relative to today, so a
        record can roll from normal -> urgent -> overdue without its own data
        changing."""
        self.search([("state", "in", OPEN_PO_STATES)])._unico_recompute_urgency()

    def action_unico_merge(self):
        """Merge RFQs without collapsing distinct purchasing lines.

        Copied from Odoo purchase.models.purchase_order.PurchaseOrder.action_merge
        for Unico's separate merge action, where we tighten the inline
        line-matching criteria. Re-diff this method against upstream Odoo when
        upgrading.

        Odoo's native merge combines lines by product/UoM/date/discount only and
        then keeps the lower unit price. For Unico, identical products may need
        separate lines when the purchase description, price, taxes, or CPQ notes
        differ, so line compatibility is checked before merging quantities.
        """
        rfq_to_merge = self.filtered(lambda r: r.state in ["draft", "sent"])

        if len(rfq_to_merge) < 2:
            raise UserError(
                _("Please select at least two purchase orders with state RFQ and RFQ sent to merge.")
            )

        rfqs_grouped = defaultdict(lambda: self.env["purchase.order"])
        for rfq in rfq_to_merge:
            key = self._prepare_grouped_data(rfq)
            rfqs_grouped[key] += rfq

        bunches_of_rfq_to_be_merge = list(rfqs_grouped.values())
        if all(len(rfq_bunch) == 1 for rfq_bunch in bunches_of_rfq_to_be_merge):
            raise UserError(
                _(
                    "In selected purchase order to merge these details must be same\n"
                    "Vendor, currency, destination, dropship address and agreement"
                )
            )
        bunches_of_rfq_to_be_merge = [
            rfqs for rfqs in bunches_of_rfq_to_be_merge if len(rfqs) > 1
        ]

        merged_rfq_ids = []

        for rfqs in bunches_of_rfq_to_be_merge:
            if len(rfqs) <= 1:
                continue
            source_group_keys = set(rfqs.mapped("unico_po_group_key"))
            oldest_rfq = min(rfqs, key=lambda r: r.date_order)
            if not oldest_rfq:
                continue

            rfqs -= oldest_rfq
            for rfq_line in rfqs.order_line:
                existing_lines = oldest_rfq.order_line.filtered(
                    lambda line: line._unico_can_merge_with_purchase_line(rfq_line)
                )
                if len(existing_lines) > 1:
                    target_line = existing_lines[0]
                    for duplicate_line in existing_lines[1:]:
                        target_line._merge_po_line(duplicate_line)
                        duplicate_line.unlink()
                    existing_lines = target_line

                if existing_lines:
                    existing_lines[0]._merge_po_line(rfq_line)
                else:
                    rfq_line.order_id = oldest_rfq

            all_origin = rfqs.mapped("origin")
            all_vendor_references = rfqs.mapped("partner_ref")

            oldest_rfq.origin = ", ".join(filter(None, [oldest_rfq.origin, *all_origin]))
            oldest_rfq.partner_ref = ", ".join(
                filter(None, [oldest_rfq.partner_ref, *all_vendor_references])
            )

            rfq_names = rfqs.mapped("name")
            merged_names = ", ".join(rfq_names)
            oldest_rfq_message = _(
                "RFQ merged with %(oldest_rfq_name)s and %(cancelled_rfq)s",
                oldest_rfq_name=oldest_rfq.name,
                cancelled_rfq=merged_names,
            )

            for rfq in rfqs:
                cancelled_rfq_message = _(
                    "RFQ merged with %s", oldest_rfq._get_html_link()
                )
                rfq.message_post(body=cancelled_rfq_message)
            oldest_rfq.message_post(body=oldest_rfq_message)

            rfqs.filtered(lambda r: r.state != "cancel").button_cancel()
            oldest_rfq._merge_alternative_po(rfqs)
            if len(source_group_keys) > 1:
                # A manually consolidated RFQ no longer belongs to one automatic
                # procurement source, so future runs must not append to it.
                oldest_rfq.unico_po_group_key = False
            merged_rfq_ids.append(oldest_rfq.id)

        action = {
            "type": "ir.actions.act_window",
            "view_mode": "list,kanban,form",
            "res_model": "purchase.order",
        }
        if len(merged_rfq_ids) == 1:
            action["res_id"] = merged_rfq_ids[0]
            action["view_mode"] = "form"
        else:
            action["name"] = _("Merged RFQs")
            action["domain"] = [("id", "in", merged_rfq_ids)]
        return action

    @api.model
    def retrieve_dashboard(self):
        """Extend the standard Purchase dashboard payload with sales-urgency
        counts so the existing banner can show Overdue / Urgent / Due-this-week
        tiles. Domains mirror the matching search filters one-for-one."""
        result = super().retrieve_dashboard()
        base = [("state", "in", list(OPEN_PO_STATES))]
        result["urgency"] = {
            "overdue": self.search_count(base + [("unico_urgency", "=", "overdue")]),
            "urgent": self.search_count(base + [("unico_urgency", "=", "urgent")]),
            "normal": self.search_count(base + [("unico_urgency", "=", "normal")]),
        }
        return result


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    def _unico_can_merge_with_purchase_line(self, rfq_line):
        """Return whether two RFQ lines are safe to collapse into one line."""
        self.ensure_one()
        rfq_line.ensure_one()

        if self.display_type or rfq_line.display_type:
            return False
        if self.product_id != rfq_line.product_id:
            return False
        if self.product_uom_id != rfq_line.product_uom_id:
            return False
        if self.analytic_distribution != rfq_line.analytic_distribution:
            return False
        if self.discount != rfq_line.discount:
            return False
        if not self._unico_merge_date_matches(rfq_line):
            return False
        if (self.name or "") != (rfq_line.name or ""):
            return False
        if set(self.tax_ids.ids) != set(rfq_line.tax_ids.ids):
            return False
        currency = self.order_id.currency_id or rfq_line.order_id.currency_id
        if float_compare(
            self.price_unit,
            rfq_line.price_unit,
            precision_rounding=currency.rounding,
        ):
            return False
        if (
            set(self.product_no_variant_attribute_value_ids.ids)
            != set(rfq_line.product_no_variant_attribute_value_ids.ids)
        ):
            return False

        for field_name in (
            "unico_purchase_note",
            "unico_purchase_cpq_note",
            "unico_cpq_special_sale_line_id",
        ):
            if field_name in self._fields and self[field_name] != rfq_line[field_name]:
                return False

        return True

    def _unico_merge_date_matches(self, rfq_line):
        self.ensure_one()
        rfq_line.ensure_one()
        if bool(self.date_planned) != bool(rfq_line.date_planned):
            return False
        if not self.date_planned:
            return True
        return abs(self.date_planned - rfq_line.date_planned).total_seconds() <= 86400
