from odoo import fields, models


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    # Picking reports whose reprints should carry a "Duplicate Copy N" watermark.
    # The count is stored on the picking and shared across both reports, so the
    # Nth print of a given transfer (by either report) is the Nth copy.
    _UNICO_WATERMARK_REPORTS = (
        "stock.report_picking",
        "unico_delivery.report_spf",
    )

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        report = self._get_report(report_ref)
        if (
            report.report_name in self._UNICO_WATERMARK_REPORTS
            and report.model == "stock.picking"
            and res_ids
        ):
            pickings = self.env["stock.picking"].browse(res_ids).exists()
            # Bump first so the value seen while rendering IS this print's copy
            # number: copy 1 is the clean original, copy 2+ shows the watermark.
            # sudo() because the printing user (e.g. portal) may lack write access
            # to this internal tracking field.
            now = fields.Datetime.now()
            for picking in pickings:
                # Stamp the original-print time exactly once (count goes 0 -> 1),
                # and refresh the last-print time on every print / reprint.
                if not picking.unico_report_print_count:
                    picking.sudo().unico_original_print_date = now
                picking.sudo().unico_last_print_date = now
                picking.sudo().unico_report_print_count += 1
        return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)
