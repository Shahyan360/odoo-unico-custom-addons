import ast
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Customers on this payment term may progress quotes without the Studio
# margin/discount approval. Resolved by name because the term carries no
# external id in this database.
NET30_PAYMENT_TERM_NAME = "Net 30 Days"

# The approval gate(s) to relax. The rule was authored in the Studio UI, so it
# is matched by the model it guards rather than an external id; every approval
# rule on sale.order is relaxed so Net 30 orders are exempt regardless of which
# button (send / confirm / ...) the rule is attached to.
_GATED_MODEL = "sale.order"


class StudioApprovalRule(models.Model):
    _inherit = "studio.approval.rule"

    @api.model
    def _apply_net30_quote_approval_bypass(self):
        """Exclude Net 30 orders from the sale.order approval rule(s).

        Called from a data ``<function>`` on every install/upgrade. The
        exclusion is injected into whatever domain a rule currently has, so any
        margin/discount thresholds configured in Studio are preserved. Safe to
        re-run: a rule that already filters on payment_term_id is left untouched.
        """
        term = self.env["account.payment.term"].search(
            [("name", "=", NET30_PAYMENT_TERM_NAME)], limit=1)
        if not term:
            _logger.warning(
                "Net 30 approval bypass skipped: payment term %r not found.",
                NET30_PAYMENT_TERM_NAME)
            return

        rules = self.search([("model_name", "=", _GATED_MODEL)])
        if not rules:
            _logger.warning(
                "Net 30 approval bypass skipped: no approval rule on %s.",
                _GATED_MODEL)
            return

        clause = ("payment_term_id", "!=", term.id)
        for rule in rules:
            domain = ast.literal_eval(rule.domain or "[]")
            # Idempotent: never stack a second payment-term clause.
            if any(isinstance(leaf, (list, tuple)) and leaf
                   and leaf[0] == "payment_term_id" for leaf in domain):
                continue
            new_domain = ["&", clause] + domain if domain else [clause]
            rule.domain = repr(new_domain)
            _logger.info(
                "Net 30 quote-approval bypass applied to rule %s (%s): %s",
                rule.id, rule.name, rule.domain)
