import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

from odoo.addons.klouded_cpq.models.cpq_special_order import _get_purchase_uom_for_product

_logger = logging.getLogger(__name__)

# CPQ single/double doors default to this many days of customer lead time,
# regardless of the product's configured Customer Lead Time (sale_delay).
UNICO_DOOR_LEAD_DAYS = 1
UNICO_DOOR_TYPE_CPQ_CODES = ("single_door", "double_door")


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    # ------------------------------------------------------------------
    # Requirement 1 - Purchase note enforcement for short-stock lines
    # ------------------------------------------------------------------
    unico_purchase_note = fields.Text(
        string="Purchase Note",
        copy=True,
        help="Sourcing / purchasing instructions for this line. Required before "
             "the order can be saved when the stock availability indicator shows "
             "a stockable product cannot be fulfilled from stock.",
    )

    unico_stock_shortfall = fields.Boolean(
        string="Stock Shortfall",
        compute="_compute_unico_stock_shortfall",
        help="True when this is a stockable line whose forecasted availability "
             "(the same figure shown by the stock availability widget) is lower "
             "than the quantity to deliver.",
    )

    @api.depends(
        "display_type",
        "is_storable",
        "is_configurable",
        "is_special_order_product",
        "display_qty_widget",
        "state",
        "is_mto",
        "qty_to_deliver",
        "free_qty_today",
        "virtual_available_at_date",
        "product_uom_id",
    )
    def _compute_unico_stock_shortfall(self):
        for line in self:
            line.unico_stock_shortfall = line._unico_stock_is_unavailable()

    def _unico_stock_is_unavailable(self):
        """Mirror the sale_stock qty-at-date widget's "forecasted issue" logic.

        Returns True for a *plain stockable* line (not a CPQ configure or
        special-order product) whose available quantity is lower than the
        quantity still to deliver, i.e. the widget shows a stock warning.
        Configure products are sourced through manufacturing / the special-order
        RFQ flow, so they are intentionally excluded.
        """
        self.ensure_one()
        if self.display_type or not self.is_storable or not self.display_qty_widget:
            return False
        if self.is_configurable or self.is_special_order_product:
            return False
        if self.is_mto:
            return False

        rounding = self.product_uom_id.rounding or 0.01
        if float_compare(self.qty_to_deliver, 0.0, precision_rounding=rounding) <= 0:
            return False

        # The widget compares against free_qty_today once the order is confirmed
        # and against the forecasted virtual availability while still a quotation.
        if self.state == "sale":
            available = self.free_qty_today
        else:
            available = self.virtual_available_at_date

        return float_compare(available, self.qty_to_deliver, precision_rounding=rounding) < 0

    @staticmethod
    def _unico_has_purchase_note(line):
        return bool(line.unico_purchase_note and line.unico_purchase_note.strip())

    @api.constrains(
        "product_id",
        "product_uom_qty",
        "product_uom_id",
        "unico_purchase_note",
        "display_type",
    )
    def _check_unico_purchase_note_for_shortfall(self):
        """On save, require a purchase note on every short-stock stockable line."""
        missing = self.env["sale.order.line"]
        for line in self:
            if line._unico_stock_is_unavailable() and not self._unico_has_purchase_note(line):
                missing |= line

        if missing:
            products = "\n".join(
                "- %s" % name
                for name in sorted({line.product_id.display_name for line in missing})
            )
            raise ValidationError(_(
                "The following stockable product(s) cannot be fulfilled from stock and "
                "require a Purchase Note before the order can be saved:\n\n%s"
            ) % products)

    # ------------------------------------------------------------------
    # Requirement 2 - Auto special RFQ creation / cleanup
    # ------------------------------------------------------------------
    def action_create_special_rfq(self):
        """Manual "Create RFQ" button - shares the create-or-update behaviour of
        the on-confirm automation: an existing draft RFQ is refreshed in place
        rather than duplicated, then the linked RFQs are opened."""
        self.ensure_one()
        self._assert_cpq_special_purchase_access()
        self._prepare_cpq_special_components_for_rfq()

        eligible = self._unico_special_purchase_components()
        if not eligible:
            raise UserError(_("There are no pending purchase components to place on an RFQ."))
        for component in eligible:
            if not component.product_id:
                raise UserError(_("Choose a product on each purchase component before creating the RFQ."))
            if not (component.vendor_id or self.cpq_special_vendor_id):
                raise UserError(_(
                    "Set a vendor on each purchase component, or set a header vendor on the sale line."
                ))

        self._unico_create_special_rfq()
        return self.action_view_special_purchases()

    @api.depends("cpq_special_purchase_ids")
    def _compute_cpq_special_purchase_count(self):
        """Preserve the CPQ field contract: count all linked special purchases.

        Sales users may read this count even when they cannot access purchase
        orders directly. Filtering to automation-owned RFQs here hides valid
        links from the CPQ sales-safe summary.
        """
        for line in self:
            line.cpq_special_purchase_count = len(line.sudo().cpq_special_purchase_ids)

    def action_view_special_purchases(self):
        """Open all RFQs/purchases linked to this line."""
        self.ensure_one()
        self._assert_cpq_special_purchase_access()
        purchases = self.sudo().cpq_special_purchase_ids
        if not purchases:
            raise UserError(_("No RFQs or purchase orders have been created for this line yet."))

        action = {
            "type": "ir.actions.act_window",
            "name": _("Special Purchase Orders"),
            "res_model": "purchase.order",
            "view_mode": "list,form",
            "domain": [("id", "in", purchases.ids)],
        }
        if len(purchases) == 1:
            action.update({"view_mode": "form", "res_id": purchases.id})
        return action

    def _unico_special_purchase_components(self):
        """Purchase-sourced components that should populate the (draft) RFQ.

        Includes components already linked to a *draft* RFQ (so a re-confirm can
        refresh prices/quantities in place) and brand-new ones, but excludes any
        component locked onto a *confirmed* purchase order - those are already
        ordered and must not be re-touched.
        """
        self.ensure_one()
        line = self.sudo()
        if "cpq_special_component_ids" not in line._fields:
            return line.env["sale.order.line.cpq.special.component"]
        return line.cpq_special_component_ids.filtered(
            lambda component: component.sourcing_method == "purchase"
            and not (
                component.purchase_line_id
                and component.purchase_line_id.order_id.state != "draft"
            )
        )

    def _unico_special_rfq_is_ready(self):
        """Whether a special-order line has everything needed to build/refresh an
        RFQ without prompting the user (all eligible purchase components have a
        product and a resolvable vendor)."""
        self.ensure_one()
        if self.display_type:
            return False

        components = self._unico_special_purchase_components()
        if not components:
            return False

        for component in components:
            if not component.product_id:
                return False
            if not (component.vendor_id or self.sudo().cpq_special_vendor_id):
                return False
        return True

    def _unico_auto_create_special_rfq(self):
        """Create the special-order RFQ for every ready line on save.

        Runs via ``sudo()`` so the RFQ is created regardless of which user saved
        the order (mirroring ``klouded_cpq``'s ``action_create_special_rfq``
        without its interactive purchase-group assertion), and never blocks the
        save: any failure is logged and posted to the sale order chatter so the
        buyer/salesperson has a visible follow-up.
        """
        for line in self:
            components = line._unico_special_purchase_components()
            if not components:
                # Only consult the (potentially large) draft-RFQ m2m when there
                # are no live components, since that is the sole case in which it
                # decides whether to run cleanup. For an active special line the
                # filter is pure wasted work on every save.
                draft_rfqs = line.sudo().cpq_special_purchase_ids.filtered(
                    lambda order: order.state == "draft"
                )
                if not draft_rfqs:
                    continue
            elif not line._unico_special_rfq_is_ready():
                continue
            try:
                with line.env.cr.savepoint():
                    line._unico_create_special_rfq(automation=True)
            except Exception as exc:  # noqa: BLE001 - automation must not break saves
                _logger.warning(
                    "unico_delivery: auto special RFQ creation failed for SO line %s: %s",
                    line.id, exc,
                )
                try:
                    line._unico_post_special_rfq_failure(exc)
                except Exception:  # noqa: BLE001 - failure notice must not break saves either
                    _logger.exception(
                        "unico_delivery: could not post special RFQ failure note for SO line %s",
                        line.id,
                    )

    def _cpq_refresh_special_rfqs(self):
        """Implement Klouded CPQ's stable downstream RFQ refresh hook."""
        self._unico_auto_create_special_rfq()
        return True

    def _unico_post_special_rfq_failure(self, error):
        """Leave a visible audit trail when the non-blocking RFQ automation fails."""
        self.ensure_one()
        order = self.order_id
        if not order:
            return
        line_label = self.name or self.product_id.display_name or _("Unnamed line")
        order.sudo().message_post(
            body=_(
                "Automatic special RFQ creation failed for line %(line)s. "
                "The sale order was saved, but purchasing still needs to create "
                "the RFQ manually. Error: %(error)s",
                line=line_label,
                error=str(error),
            ),
            subtype_xmlid="mail.mt_note",
        )

    def _unico_post_special_rfq_cleanup_failure(self, purchases, error, sale_order=False, rfq_names=False):
        """Leave a specific note when draft special-RFQ cleanup fails."""
        order = sale_order or (self.order_id if len(self) == 1 else False)
        if not order:
            return
        if not rfq_names:
            rfq_names = ", ".join(purchases.exists().mapped("name"))
        order.sudo().message_post(
            body=_(
                "Automatic special RFQ cleanup could not remove stale draft RFQ(s) "
                "%(rfqs)s. The sale order was saved, but no unsafe RFQ cleanup "
                "was performed. Purchasing should review the stale RFQ manually. "
                "Error: %(error)s",
                rfqs=rfq_names or _("Unknown RFQ"),
                error=str(error),
            ),
            subtype_xmlid="mail.mt_note",
        )

    def _unico_post_special_rfq_review_note(self, purchases, body, sale_order=False):
        """Leave a neutral 'please review' note (not a failure) on the sale order.

        Used when automation deliberately leaves a line/RFQ in place for a human
        to look at -- a stale draft released after a vendor change, or a
        same-product line it did not create -- rather than touching content it
        cannot prove it owns.
        """
        order = sale_order or (self.order_id if len(self) == 1 else False)
        if not order:
            return
        rfq_names = ", ".join(purchases.exists().mapped("name")) or _("Unknown RFQ")
        order.sudo().message_post(
            body=_("%(body)s (RFQ %(rfqs)s)", body=body, rfqs=rfq_names),
            subtype_xmlid="mail.mt_note",
        )

    def _unico_rfq_has_unowned_lines(self, purchase, owned_purchase_line_ids=False):
        """Return whether a draft RFQ contains buyer/manual/other-line content."""
        if owned_purchase_line_ids is not False:
            owned_purchase_line_ids = set(owned_purchase_line_ids)
            return bool(purchase.order_line.filtered(
                lambda po_line: po_line.id not in owned_purchase_line_ids
            ))
        owner_line_ids = set(self.ids)
        return bool(purchase.order_line.filtered(
            lambda po_line: not po_line.unico_cpq_special_sale_line_id
            or po_line.unico_cpq_special_sale_line_id.id not in owner_line_ids
        ))

    def _unico_cleanup_draft_special_rfqs(
        self,
        purchases,
        sale_order=False,
        raise_on_failure=False,
        owned_purchase_line_ids_by_rfq=False,
    ):
        """Cancel/delete linked draft special RFQs.

        Both automatic saves and the manual "Create RFQ" button create draft
        RFQs that remain tied to this sale line. If the vendor changes or the
        line is deleted, leaving purely automation-owned drafts alive can
        double-order material. Drafts containing buyer/manual lines are never
        deleted automatically; purchasing must review those explicitly.
        Confirmed/non-draft RFQs are deliberately left untouched.
        """
        purchases = purchases.sudo().filtered(lambda order: order.state == "draft")
        if not purchases:
            return purchases, purchases
        cleaned = self.env["purchase.order"]
        failed = self.env["purchase.order"]
        for purchase in purchases:
            rfq_name = purchase.name or _("Unknown RFQ")
            owned_purchase_line_ids = (
                owned_purchase_line_ids_by_rfq
                and owned_purchase_line_ids_by_rfq.get(purchase.id, False)
            )
            if self._unico_rfq_has_unowned_lines(
                purchase,
                owned_purchase_line_ids=owned_purchase_line_ids,
            ):
                failed |= purchase
                error = _(
                    "Draft RFQ %(rfq)s contains manual or unverified purchase lines, "
                    "so it was left for purchasing to review.",
                    rfq=rfq_name,
                )
                _logger.warning("unico_delivery: %s", error)
                if raise_on_failure:
                    raise UserError(error)
                self._unico_post_special_rfq_cleanup_failure(
                    purchase,
                    error,
                    sale_order=sale_order,
                    rfq_names=rfq_name,
                )
                continue
            try:
                purchase.button_cancel()
                purchase.unlink()
                cleaned |= purchase
            except Exception as exc:  # noqa: BLE001 - cleanup must not block automatic order editing
                failed |= purchase.exists()
                _logger.warning(
                    "unico_delivery: draft special RFQ cleanup failed for RFQ %s (%s): %s",
                    rfq_name, purchase.id, exc,
                )
                if raise_on_failure:
                    raise UserError(_(
                        "Draft special RFQ %(rfq)s could not be removed. "
                        "Cancel or delete that RFQ manually before creating the replacement RFQ. "
                        "Error: %(error)s",
                        rfq=rfq_name,
                        error=str(exc),
                    )) from exc
                self._unico_post_special_rfq_cleanup_failure(
                    purchase,
                    exc,
                    sale_order=sale_order,
                    rfq_names=rfq_name,
                )
        return cleaned, failed

    def _unico_rfqs_linked_to_other_sale_lines(self, purchases):
        """Return RFQs linked to any sale line outside ``self``."""
        purchases = purchases.sudo().exists()
        if not purchases:
            return purchases
        other_lines = self.env["sale.order.line"].sudo().search([
            ("id", "not in", self.ids),
            ("cpq_special_purchase_ids", "in", purchases.ids),
        ])
        other_components = self.env[
            "sale.order.line.cpq.special.component"
        ].sudo().search([
            ("sale_line_id", "not in", self.ids),
            "|",
            ("purchase_order_id", "in", purchases.ids),
            ("purchase_line_id.order_id", "in", purchases.ids),
        ])
        other_lines |= other_components.mapped("sale_line_id")
        linked_orders = other_lines._unico_linked_special_purchases()
        return purchases.filtered(lambda order: order in linked_orders)

    def _unico_linked_special_purchases(self):
        """Return RFQs linked by either the line M2M or its components.

        Older and partially repaired quotations can have a stale
        ``cpq_special_purchase_ids`` relation while the component still holds
        the authoritative PO/PO-line link.  Treat every ORM relationship as a
        live reference so deletion cleanup cannot leave an orphan draft RFQ.
        """
        lines = self.sudo().exists()
        purchases = lines.mapped("cpq_special_purchase_ids")
        components = lines.mapped("cpq_special_component_ids")
        purchases |= components.mapped("purchase_order_id")
        purchases |= components.mapped("purchase_line_id.order_id")
        return purchases.exists()

    def _unico_release_special_rfq(self, rfq, our_component_lines, shared=False, note_body=False):
        """Release this sale line's claim on a draft RFQ it no longer needs.

        Removes only the lines that genuinely belong to this sale line -- marked
        ``unico_cpq_special_sale_line_id == self`` or pointed at by one of the
        current components -- so the same material can never be ordered twice.
        Buyer-added and other content is always preserved. A draft left holding
        nothing but our lines is cancelled and deleted; otherwise the leftover is
        kept and a review note is posted. A *shared* draft (relied on by another
        sale line) keeps everything except this line's exclusively-marked lines,
        which no other line can own. Never raises.
        """
        self.ensure_one()
        if shared:
            rfq.order_line.filtered(
                lambda po_line: po_line.unico_cpq_special_sale_line_id == self
            ).sudo().unlink()
            self._unico_post_special_rfq_review_note(rfq, note_body, sale_order=self.order_id)
            return
        ours = rfq.order_line.filtered(
            lambda po_line: po_line.unico_cpq_special_sale_line_id == self
            or po_line in our_component_lines
        )
        others = rfq.order_line.filtered(
            lambda po_line: po_line not in ours
            and not getattr(po_line, "display_type", False)
        )
        if not others:
            try:
                rfq.button_cancel()
                rfq.unlink()
                return
            except Exception as exc:  # noqa: BLE001 - cleanup must not block ordering
                _logger.warning(
                    "unico_delivery: draft special RFQ %s could not be removed: %s",
                    rfq.name, exc,
                )
        ours.sudo().unlink()
        self._unico_post_special_rfq_review_note(rfq, note_body, sale_order=self.order_id)

    def _unico_prepare_special_rfq_line_vals(
        self,
        component,
        vendor=False,
        sequence=10,
        purchase_order=False,
    ):
        product = component.product_id
        uom = component.product_uom_id or _get_purchase_uom_for_product(
            product, component.vendor_id or vendor
        )
        purchase_order = purchase_order or component.sudo().purchase_order_id
        company = (
            purchase_order.company_id
            or self.company_id
            or self.order_id.company_id
            or self.env.company
        )
        purchase_currency = purchase_order.currency_id or company.currency_id
        purchase_date = (
            fields.Date.to_date(purchase_order.date_order)
            if purchase_order and purchase_order.date_order
            else fields.Date.context_today(self)
        )
        quoted_price = component._get_cpq_vendor_quote_unit_cost(
            target_currency=purchase_currency,
            company=company,
            date=purchase_date,
        )
        return {
            "sequence": component.sequence or sequence,
            "name": component.name or product.display_name,
            "product_id": product.id,
            "product_qty": self._get_cpq_special_component_purchase_qty(component),
            "product_uom_id": uom.id,
            "price_unit": quoted_price,
            "date_planned": fields.Datetime.now(),
            "unico_cpq_special_sale_line_id": component.sale_line_id.id,
        }

    def _unico_find_marked_special_rfq_line(self, purchase_order, component, excluded_lines=False):
        """Find an existing RFQ line already marked as owned by this sale line."""
        self.ensure_one()
        if not purchase_order or not component.product_id:
            return self.env["purchase.order.line"]
        excluded_lines = excluded_lines or self.env["purchase.order.line"]
        candidates = purchase_order.order_line.filtered(
            lambda po_line: po_line not in excluded_lines
            and po_line.unico_cpq_special_sale_line_id == self
            and not getattr(po_line, "display_type", False)
            and po_line.product_id == component.product_id
        )
        return candidates[:1] if len(candidates) == 1 else self.env["purchase.order.line"]

    def _unico_find_unverified_special_rfq_lines(self, purchase_order, components, excluded_lines=False):
        """Return unmarked same-product RFQ lines that cannot be safely updated."""
        self.ensure_one()
        if not purchase_order or not components:
            return self.env["purchase.order.line"]
        excluded_lines = excluded_lines or self.env["purchase.order.line"]
        component_products = components.mapped("product_id")
        if not component_products:
            return self.env["purchase.order.line"]
        return purchase_order.order_line.filtered(
            lambda po_line: po_line not in excluded_lines
            and not po_line.unico_cpq_special_sale_line_id
            and not getattr(po_line, "display_type", False)
            and po_line.product_id in component_products
        )

    def _unico_create_special_rfq(self, automation=False):
        """Create or update the special-order RFQ(s) for the line's pending
        purchase components, grouped by vendor.

        Ownership model: the automation owns exactly the purchase lines marked
        ``unico_cpq_special_sale_line_id == self`` (or directly linked through a
        component's ``purchase_line_id``). It freely creates, updates and removes
        only those lines, and never edits or deletes buyer-added, other-line or
        unmarked legacy lines.

        It always (re)builds the current vendors' RFQs so the live component
        quantities/prices are ordered -- a stale RFQ for an old vendor, or a
        same-product line it cannot prove it owns, never blocks that. When it has
        to leave such a line behind (a stale draft it cannot fully remove, or a
        possible duplicate), it posts a review note instead of refusing to order.
        Confirmed RFQs are left untouched. Self-contained and runs as ``sudo()``,
        mirroring ``klouded_cpq``'s creation otherwise."""
        self.ensure_one()
        line = self.sudo()
        line._lock_cpq_custom_door_lines()

        existing_draft = line.cpq_special_purchase_ids.filtered(
            lambda order: order.state == "draft"
        )
        eligible = line._unico_special_purchase_components()
        if not eligible:
            # Nothing left to purchase: release this line's owned lines from every
            # linked draft RFQ so stale demand cannot be confirmed. A draft left
            # holding only our lines is cancelled and deleted; one with buyer/
            # manual lines is kept (stripped of our lines) for review. Either way
            # the released drafts are unlinked from the sale line.
            if existing_draft:
                shared = line._unico_rfqs_linked_to_other_sale_lines(existing_draft)
                for rfq in existing_draft:
                    line._unico_release_special_rfq(
                        rfq,
                        line.env["purchase.order.line"],
                        shared=rfq in shared,
                        note_body=_("No purchasable CPQ special components remain "
                                    "on this sale line; this draft RFQ was released "
                                    "for purchasing to review."),
                    )
                line.cpq_special_purchase_ids = [
                    (6, 0, (line.cpq_special_purchase_ids - existing_draft).ids)
                ]
            return line.env["purchase.order"]

        components_by_vendor = {}
        for component in eligible:
            vendor = component.vendor_id or line.cpq_special_vendor_id
            components_by_vendor.setdefault(
                vendor.id, line.env["sale.order.line.cpq.special.component"]
            )
            components_by_vendor[vendor.id] |= component

        # --- Decouple stale drafts (vendor no longer required) ------------
        # A draft RFQ whose vendor is not on any current component is stale.
        # Delete it outright only when it is purely ours and unshared; otherwise
        # strip just our owned lines (so it cannot double-order the material the
        # new vendor's RFQ will carry) and leave the rest for review. Either way,
        # creation of the current vendors' RFQs below always proceeds.
        # Lines that genuinely belong to this sale line: marked as ours, or
        # directly pointed at by one of the current components. Removing only
        # these can never touch a buyer-added or another line's purchase line.
        our_component_lines = eligible.mapped("purchase_line_id")

        dropped_links = line.env["purchase.order"]
        stale = existing_draft.filtered(
            lambda order: order.partner_id.id not in components_by_vendor
        )
        if stale:
            shared_stale = line._unico_rfqs_linked_to_other_sale_lines(stale)
            for rfq in stale:
                line._unico_release_special_rfq(
                    rfq,
                    our_component_lines,
                    shared=rfq in shared_stale,
                    note_body=_("Vendor changed; this stale draft RFQ was left "
                                "for purchasing to review."),
                )
            dropped_links = stale
            existing_draft = (existing_draft - stale).exists()

        # --- Build / refresh each current vendor's RFQ --------------------
        used_orders = line.env["purchase.order"]
        review_orders = line.env["purchase.order"]
        for vendor_id, components in components_by_vendor.items():
            vendor = line.env["res.partner"].browse(vendor_id)
            ordered_components = components.sorted(
                key=lambda component: (component.sequence, component.id)
            )
            vendor_drafts = existing_draft.filtered(
                lambda order: order.partner_id.id == vendor_id
            )
            existing = vendor_drafts[:1]
            extra_drafts = vendor_drafts - existing
            if extra_drafts:
                # Refresh one draft below; release our lines from the duplicate
                # same-vendor draft(s) so the component cannot be ordered twice.
                shared_extra = line._unico_rfqs_linked_to_other_sale_lines(extra_drafts)
                for rfq in extra_drafts:
                    line._unico_release_special_rfq(
                        rfq,
                        our_component_lines,
                        shared=rfq in shared_extra,
                        note_body=_("More than one draft RFQ exists for this vendor; "
                                    "automation refreshed one and released the "
                                    "other(s) for purchasing to review."),
                    )
                dropped_links |= extra_drafts
            if existing:
                purchase_order = existing
            else:
                purchase_order = line.env["purchase.order"].create({
                    "partner_id": vendor.id,
                    "origin": "%s / %s" % (
                        line.order_id.name or _("Sales Order"),
                        line.name or line.product_id.display_name,
                    ),
                    "company_id": line.company_id.id or line.env.company.id,
                    "unico_auto_special_rfq": bool(automation),
                })
            used_orders |= purchase_order
            if purchase_order.unico_auto_special_rfq and not automation:
                purchase_order.write({"unico_auto_special_rfq": False})

            current_purchase_lines = line.env["purchase.order.line"]
            for index, component in enumerate(ordered_components):
                # Reuse the component's own draft line only if it is unmarked or
                # already ours -- never hijack a line owned by another sale line.
                purchase_line = component.purchase_line_id.exists().filtered(
                    lambda po_line: po_line.order_id == purchase_order
                    and po_line.order_id.state == "draft"
                    and (not po_line.unico_cpq_special_sale_line_id
                         or po_line.unico_cpq_special_sale_line_id == line)
                )[:1]
                if not purchase_line:
                    purchase_line = line._unico_find_marked_special_rfq_line(
                        purchase_order,
                        component,
                        excluded_lines=current_purchase_lines,
                    )
                line_vals = line._unico_prepare_special_rfq_line_vals(
                    component,
                    vendor=vendor,
                    sequence=(index + 1) * 10,
                    purchase_order=purchase_order,
                )
                if purchase_line:
                    purchase_line.write(line_vals)
                else:
                    purchase_line = line.env["purchase.order.line"].create({
                        "order_id": purchase_order.id,
                        **line_vals,
                    })
                component.write({
                    "purchase_order_id": purchase_order.id,
                    "purchase_line_id": purchase_line.id,
                })
                current_purchase_lines |= purchase_line

            component_owned_lines = purchase_order.order_line.filtered(
                lambda po_line: po_line.unico_cpq_special_sale_line_id == line
            )
            obsolete_component_lines = (
                component_owned_lines - current_purchase_lines
            ).filtered(lambda po_line: po_line.order_id.state == "draft")
            if obsolete_component_lines:
                obsolete_component_lines.sudo().unlink()

            # Same-product lines we did not create (unmarked legacy / buyer):
            # left untouched, but flag the RFQ so purchasing can dedupe.
            if line._unico_find_unverified_special_rfq_lines(
                purchase_order, components, excluded_lines=current_purchase_lines,
            ):
                review_orders |= purchase_order

        if review_orders:
            line._unico_post_special_rfq_review_note(
                review_orders,
                _("This RFQ has same-product line(s) that automation did not "
                  "create. Review them for possible duplicates."),
                sale_order=line.order_id,
            )

        linked_orders = (line.cpq_special_purchase_ids - dropped_links) | used_orders
        line.cpq_special_purchase_ids = [(6, 0, linked_orders.ids)]
        return used_orders

    def unlink(self):
        """When a special-order line is removed, delete any still-draft RFQ
        linked only to that line."""
        candidate_rfqs = self.env["purchase.order"]
        sale_order_by_rfq = {}
        owned_purchase_line_ids_by_rfq = {}
        if "cpq_special_purchase_ids" in self._fields:
            for line in self.sudo():
                components = line.cpq_special_component_ids
                line_candidate_rfqs = line._unico_linked_special_purchases().filtered(
                    lambda order: order.state == "draft"
                )
                candidate_rfqs |= line_candidate_rfqs
                for rfq in line_candidate_rfqs:
                    sale_order_by_rfq.setdefault(rfq.id, line.order_id)
                    marked_lines = rfq.order_line.filtered(
                        lambda po_line: (
                            po_line.unico_cpq_special_sale_line_id == line
                        )
                    )
                    direct_component_lines = components.mapped(
                        "purchase_line_id"
                    ).filtered(lambda po_line: po_line.order_id == rfq)
                    # A direct component link is authoritative for discovering
                    # the RFQ, but an unmarked line on a manually created RFQ is
                    # still buyer-owned and must survive for review.  Only treat
                    # the direct link itself as owned when automation created the
                    # RFQ; explicit line ownership markers remain authoritative
                    # for both automated and manually requested CPQ RFQs.
                    if rfq.unico_auto_special_rfq:
                        marked_lines |= direct_component_lines
                    owned_purchase_line_ids_by_rfq.setdefault(rfq.id, set()).update(
                        marked_lines.ids
                    )

        # This override already reconciles the linked draft RFQs below from a
        # pre-delete snapshot; suppress the per-component reconcile that would
        # otherwise fire as the cascade deletes cpq_special_component_ids.
        result = super(
            SaleOrderLine, self.with_context(unico_skip_component_rfq_trigger=True)
        ).unlink()

        existing_candidate_rfqs = candidate_rfqs.exists()
        if existing_candidate_rfqs:
            linked_orders = self._unico_rfqs_linked_to_other_sale_lines(
                existing_candidate_rfqs
            )
            cleanup_rfqs = existing_candidate_rfqs - linked_orders
            cleanup_proxy = self.sudo()
            for cleanup_rfq in cleanup_rfqs:
                cleanup_proxy._unico_cleanup_draft_special_rfqs(
                    cleanup_rfq,
                    sale_order=sale_order_by_rfq.get(cleanup_rfq.id),
                    owned_purchase_line_ids_by_rfq=owned_purchase_line_ids_by_rfq,
                )

        return result

    # ------------------------------------------------------------------
    # Customer lead time contribution (drives the sale order Delivery Date).
    # ------------------------------------------------------------------
    def _unico_is_cpq_single_or_double_door(self):
        """True when this configured CPQ line is a single or double door."""
        self.ensure_one()
        if not self.is_configurable or not self.cpq_configuration_data:
            return False
        try:
            config = json.loads(self.cpq_configuration_data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        value_id = config.get("door_type")
        if not isinstance(value_id, int):
            return False
        value = self.env["product.attribute.value"].browse(value_id)
        return value.exists() and value.cpq_code in UNICO_DOOR_TYPE_CPQ_CODES

    def _unico_customer_lead_time(self):
        """Customer lead time (in days) this line contributes to the order's
        Delivery Date. Single/double door CPQ lines default to 1 day; every
        other line uses the product's Customer Lead Time (``sale_delay``)."""
        self.ensure_one()
        if not self.product_id:
            return 0
        if self._unico_is_cpq_single_or_double_door():
            return UNICO_DOOR_LEAD_DAYS
        return self.product_id.sale_delay
