from odoo import models


class StockRule(models.Model):
    _inherit = "stock.rule"

    # -- Per-source RFQ grouping -------------------------------------------
    # Odoo's default `_run_buy` merges every buy procurement that shares a
    # vendor (plus buyer / currency / picking type / company) into a single
    # draft PO, only sub-splitting by reference when the vendor's "Group RFQ"
    # is "On Order". That collapses unrelated reordering rules onto one RFQ,
    # e.g. a PO whose Source Document is "OP/00016, OP/00019, OP/00007, ...".
    #
    # When the company setting "Group RFQs per source" is on (default), we
    # group strictly per source, regardless of the vendor's Group RFQ setting:
    #   * direct MTO sale procurement -> one PO per sale order;
    #   * orderpoint replenishment     -> one PO per reordering rule (OP), even
    #     when several sales contribute demand for the same product.
    # Buyers still combine POs afterwards with the manual "Merge POs" action.

    def _make_po_get_domain(self, company_id, values, partner):
        domain = super()._make_po_get_domain(company_id, values, partner)
        if not self._unico_group_rfq_per_source(company_id):
            return domain
        group_key = self._unico_po_group_key(values)
        return domain + (("unico_po_group_key", "=", group_key),)

    def _prepare_purchase_order(self, company_id, origins, values):
        res = super()._prepare_purchase_order(company_id, origins, values)
        if self._unico_group_rfq_per_source(company_id):
            # All procurements grouped here share the same source, so any of
            # their values yields the same key; the first is representative.
            res["unico_po_group_key"] = self._unico_po_group_key(values[0])
        return res

    def _unico_group_rfq_per_source(self, company_id):
        """Whether per-source RFQ grouping is enabled (Purchase settings).

        Read through the company record (ORM), so it is always consistent with
        the current transaction — no ir.config_parameter cache to go stale.
        """
        company = company_id or self.company_id or self.env.company
        return bool(company.sudo().unico_group_rfq_per_source)

    def _unico_po_group_key(self, values):
        """Return the grouping key for the draft PO this procurement belongs to.

        ``False`` falls back to Odoo's default (vendor-level) grouping.
        """
        # Reorder procurements carry a key stamped by the orderpoint (works for
        # both auto and manual rules, where enterprise drops ``orderpoint_id``).
        stamped = values.get("unico_source_group_key")
        if stamped:
            return stamped
        # MTO / sale-driven buy procurements: one PO per sale order.
        sale_orders = self._unico_procurement_sale_orders(values)
        if sale_orders:
            return "so:" + ",".join(str(so_id) for so_id in sorted(sale_orders.ids))
        # Auto orderpoints still expose the record directly.
        orderpoint = values.get("orderpoint_id")
        if orderpoint:
            return "op:%s" % orderpoint.id
        return False

    def _unico_procurement_sale_orders(self, values):
        """Sale order(s) a procurement is raised for, across the paths that
        carry the link: the stock reference, the destination move chain, or the
        procurement group."""
        sale_orders = self.env["sale.order"]

        references = values.get("reference_ids")
        if self._unico_has_field(references, "sale_ids"):
            sale_orders |= references.sale_ids

        if not sale_orders:
            moves = values.get("move_dest_ids")
            if self._unico_has_field(moves, "sale_line_id"):
                sale_orders |= moves.sale_line_id.order_id

        if not sale_orders:
            group = values.get("group_id")
            if self._unico_has_field(group, "sale_id"):
                sale_orders |= group.sale_id

        return sale_orders

    @staticmethod
    def _unico_has_field(record, field_name):
        """True only for a non-empty recordset exposing ``field_name`` — guards
        against list/id procurement values so the scheduler never crashes."""
        return bool(record) and field_name in getattr(record, "_fields", {})
