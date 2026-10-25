import re

from odoo import models

# What counts as a Net 30 term. The Unico database holds several spellings
# ("Net 30 Days", "NET 30", "net30"), and users keep adding more, so the term is
# recognised by pattern rather than by an exact name or an external id.
# ``\b`` after the 30 keeps "Net 300 Days" out.
NET30_NAME_PATTERN = re.compile(r"net\s*-?\s*30\b", re.IGNORECASE)


class AccountPaymentTerm(models.Model):
    _inherit = "account.payment.term"

    def _unico_is_net30(self):
        """Whether this payment term is a Net 30 term.

        Both the source (en_US) name and the reader's translation are checked,
        so the automation behaves the same whichever language the term was
        entered or is being read in.
        """
        self.ensure_one()
        names = (self.name or "", self.with_context(lang="en_US").name or "")
        return any(NET30_NAME_PATTERN.search(name) for name in names)
