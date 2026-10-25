import ast
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Code-versioned mail template (see data/quote_internal_approval_email.xml).
_APPROVAL_TEMPLATE = "unico_sale_net30_approval.quote_internal_approval_email"

# The Studio approval rule(s) whose approvers should receive the request. Matched
# by the model they guard, not an external id (they were authored in Studio).
_GATED_MODEL = "sale.order"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ------------------------------------------------------------------
    # Customer PO requirement
    # ------------------------------------------------------------------
    # Some customers will not pay an invoice that does not quote their own
    # purchase order number, so the PO has to be captured before the order is
    # worked rather than discovered at billing time.
    unico_po_detail = fields.Char(
        string="PO Detail",
        copy=False,
        help="The customer's own purchase order reference for this sale. "
             "Required before confirmation when the customer is flagged with "
             "PO Alert.",
    )
    unico_po_required = fields.Boolean(
        string="PO Required",
        compute="_compute_unico_po_required",
        help="True when this order's customer is flagged with PO Alert, so the "
             "order cannot be confirmed without PO Detail. Drives the field's "
             "required marker on the form.",
    )

    @api.depends("partner_id", "partner_id.commercial_partner_id.unico_po_alert")
    def _compute_unico_po_required(self):
        for order in self:
            order.unico_po_required = order._unico_po_detail_is_required()

    def _unico_po_detail_is_required(self):
        """Whether this order's customer demands their PO on the order.

        Read from the *commercial* partner so the flag can be set once on a
        company and cover every contact billing under it, rather than having to
        be ticked on each contact individually.
        """
        self.ensure_one()
        return bool(self.partner_id.commercial_partner_id.unico_po_alert)

    def action_confirm(self):
        # Checked at confirmation rather than on save: a quotation is often
        # built before the customer has issued their PO, and blocking the save
        # would stop it being quoted at all.
        missing = self.filtered(
            lambda order: order._unico_po_detail_is_required()
            and not (order.unico_po_detail or "").strip()
        )
        if missing:
            raise UserError(_(
                "These customers require their purchase order details before "
                "an order can be confirmed. Fill in PO Detail on:\n\n%s",
                "\n".join(
                    "- %s (%s)" % (order.name, order.partner_id.display_name)
                    for order in missing),
            ))
        return super().action_confirm()

    # ------------------------------------------------------------------
    # Internal quote approval request
    # ------------------------------------------------------------------
    def action_request_internal_approval(self):
        """Email the quote's approvers asking them to review/approve it.

        Renders the ``Quotation Approval Internal`` template and sends it to the
        Approvers (+ Users to Notify) of the Studio approval rule(s) on
        ``sale.order`` that this order actually triggers (its domain matches).
        From-address is the company email, falling back to the current user
        (set on the template). Safe to call on any order — it never emails the
        customer: if no internal approver resolves, it logs a chatter note and
        sends nothing.

        Intended to be invoked from a server action / automation tied to the
        approval flow. Do NOT register this method name in the Studio rule's
        *Method* field — that field gates the method (requires approval to run
        it), which would be circular. Keep the gate on ``action_quotation_send``
        and call this from the rule's *Action* (server action) instead.
        """
        template = self.env.ref(_APPROVAL_TEMPLATE, raise_if_not_found=False)
        if not template:
            _logger.warning(
                "Internal approval email skipped: template %r not found.",
                _APPROVAL_TEMPLATE)
            return False

        for order in self:
            recipients = order._internal_approval_recipients()
            if not recipients:
                order.message_post(body=_(
                    "Internal approval email not sent: no approver with an email "
                    "could be resolved from the Studio approval rule(s) on "
                    "sale.order."))
                continue
            template.sudo().send_mail(
                order.id,
                force_send=False,
                email_values={"recipient_ids": [(6, 0, recipients.ids)]},
            )
            order.message_post(body=_(
                "Internal approval requested by email from: %s",
                ", ".join(recipients.mapped("display_name"))))
        return True

    def _internal_approval_recipients(self):
        """Partners to notify: the Approvers and Users-to-Notify of every active
        ``sale.order`` Studio approval rule this order triggers.

        A rule applies when the order satisfies the rule's domain (an empty/
        unparseable domain applies to all). Only partners that actually have an
        email are returned, so a misconfigured approver never produces a silent
        send-to-nobody.

        This is an INTERNAL approval request, so the recipient set is twice
        guarded against ever reaching the customer:
          1. Only internal users (``share == False``) are kept — portal/customer
             users are dropped even if mistakenly added as approvers.
          2. The order's own customer/invoice/delivery partners (and anyone
             under the same commercial entity) are explicitly removed.
        """
        self.ensure_one()
        rules = self.env["studio.approval.rule"].sudo().search(
            [("model_name", "=", _GATED_MODEL)])
        users = self.env["res.users"]
        for rule in rules:
            try:
                domain = ast.literal_eval(rule.domain or "[]")
                if domain and not self.filtered_domain(domain):
                    continue
            except (ValueError, SyntaxError, KeyError):
                # Unparseable / field-mismatched domain: fall through and notify,
                # rather than silently dropping the approval request.
                pass
            users |= rule.approver_ids | rule.users_to_notify

        # Guard 1: internal staff only — never a portal/customer-facing user.
        partners = users.filtered(lambda u: not u.share).partner_id.filtered("email")

        # Guard 2: never the order's customer or any contact of the same company.
        customer = self.partner_id.commercial_partner_id
        forbidden = self.partner_id | self.partner_invoice_id | self.partner_shipping_id
        return partners.filtered(
            lambda p: p not in forbidden
            and (not customer or p.commercial_partner_id != customer))
