from odoo import models


class SaleOrderLineCpqSpecialComponent(models.Model):
    _inherit = "sale.order.line.cpq.special.component"

    def unlink(self):
        """Release stale special-RFQ demand when a purchase component is removed.

        Removing a component otherwise leaves its automation-owned draft RFQ line
        live and confirmable by purchasing. After deletion, reconcile the special
        RFQ for the affected sale lines so a draft left holding only our lines is
        cancelled and deleted, while buyer/manual lines are kept for review.

        Skipped in two cases that already reconcile by themselves:
        - the configurator is mid-edit (``cpq_skip_special_bom_sync``): its
          confirm step calls the CPQ RFQ refresh hook once everything
          has settled, so triggering here would be premature/duplicate;
        - the parent sale line is being deleted (``unico_skip_component_rfq_trigger``
          set by ``sale.order.line.unlink``): that override cleans up the RFQ from
          a pre-delete ownership snapshot.
        ``unlink`` is never called by the RFQ automation itself, so this cannot
        recurse.
        """
        sale_lines = self.mapped("sale_line_id")
        result = super().unlink()
        if not (
            self.env.context.get("cpq_skip_special_bom_sync")
            or self.env.context.get("unico_skip_component_rfq_trigger")
        ):
            sale_lines.exists()._cpq_refresh_special_rfqs()
        return result
