import base64
import csv
import io

from odoo import _, fields, models
from odoo.exceptions import UserError


class RmaLegacyEntitlementImport(models.TransientModel):
    _name = "rma.legacy.entitlement.import"
    _description = "Import Original Sage Sale Lines"

    file_data = fields.Binary("CSV File", required=True)
    filename = fields.Char("Filename")
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    migration_cutoff_date = fields.Date(
        "Migration Cutoff Date",
        required=True,
        help="Last Sage transaction date included in this import.",
    )
    update_existing = fields.Boolean(
        "Update Existing Sale Lines",
        help="Match existing imported sale lines by Company + Sage Line ID.",
    )

    def action_import(self):
        self.ensure_one()
        rows = self._read_csv_rows()
        Entitlement = self.env["rma.legacy.entitlement"]
        created = Entitlement
        updated = Entitlement

        for row_number, row in rows:
            vals = self._prepare_entitlement_vals(row, row_number)
            existing = Entitlement.search([
                ("company_id", "=", self.company_id.id),
                ("sage_line_ref", "=", vals["sage_line_ref"]),
            ], limit=1)
            if existing:
                if not self.update_existing:
                    raise UserError(
                        _("Row %(row)s is already imported as Sage line %(line)s.")
                        % {"row": row_number, "line": vals["sage_line_ref"]}
                    )
                existing.write(vals)
                updated |= existing
            else:
                created |= Entitlement.create(vals)

        message = _(
            "Imported Sage sale lines: %(created)s created, %(updated)s updated."
        ) % {
            "created": len(created),
            "updated": len(updated),
        }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sage Sale Lines Imported"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def _read_csv_rows(self):
        decoded = base64.b64decode(self.file_data or b"")
        text = decoded.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise UserError(_("The Sage sale line import file is empty."))

        rows = []
        for row_number, row in enumerate(reader, start=2):
            normalized = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in row.items()
            }
            if any(normalized.values()):
                rows.append((row_number, normalized))
        if not rows:
            raise UserError(_("The Sage sale line import file has no data rows."))
        return rows

    def _prepare_entitlement_vals(self, row, row_number):
        product = self._match_product(row, row_number)
        partner = self._match_partner(row, row_number)
        sage_order_ref = self._first_value(row, "sage_order_ref", "order_ref", "order")
        sage_invoice_ref = self._first_value(row, "sage_invoice_ref", "invoice_ref", "invoice")
        sage_line_ref = self._get_sage_line_ref(row, row_number, sage_invoice_ref, sage_order_ref)

        vals = {
            "company_id": self.company_id.id,
            "partner_id": partner.id,
            "product_id": product.id,
            "sage_order_ref": sage_order_ref,
            "sage_invoice_ref": sage_invoice_ref,
            "sage_line_ref": sage_line_ref,
            "invoice_date": self._parse_date(
                self._first_value(row, "invoice_date", "sage_invoice_date"),
                row_number,
            ),
            "migration_cutoff_date": self.migration_cutoff_date,
            "qty_sold": self._parse_float(row, row_number, "qty_sold", "quantity_sold", "sold_qty"),
            "qty_returned": self._parse_float(
                row,
                row_number,
                "qty_returned",
                "already_returned_qty",
                "returned_qty",
                default=0.0,
            ),
            "price_unit": self._parse_float(
                row,
                row_number,
                "unit_price",
                "price_unit",
                "sell_price",
                default=0.0,
            ),
            "unit_cost": self._parse_float(
                row,
                row_number,
                "unit_cost",
                "cost",
                default=0.0,
            ),
            "tax_code": self._first_value(row, "tax_code", "sage_tax_code"),
            "imported_filename": self.filename,
            "import_note": self._first_value(row, "note", "notes"),
        }

        refund_account = self._match_refund_account(row, row_number)
        if refund_account:
            vals["refund_account_id"] = refund_account.id

        taxes = self._match_taxes(row, row_number)
        if taxes:
            vals["tax_ids"] = [(6, 0, taxes.ids)]

        lot = self._match_or_create_lot(row, row_number, product)
        if lot:
            vals["lot_id"] = lot.id

        return vals

    def _first_value(self, row, *names):
        for name in names:
            value = row.get(name)
            if value:
                return value
        return False

    def _get_sage_line_ref(self, row, row_number, sage_invoice_ref, sage_order_ref):
        sage_line_ref = self._first_value(row, "sage_line_ref", "line_ref", "external_line_ref")
        if sage_line_ref:
            return sage_line_ref

        line_number = self._first_value(row, "line_number", "line_no", "sage_line_number")
        document_ref = sage_invoice_ref or sage_order_ref
        if document_ref and line_number:
            return "%s:%s" % (document_ref, line_number)
        raise UserError(
            _(
                "Row %(row)s needs sage_line_ref, or an invoice/order reference plus line_number."
            ) % {"row": row_number}
        )

    def _parse_float(self, row, row_number, *names, default=None):
        value = self._first_value(row, *names)
        if not value:
            if default is not None:
                return default
            raise UserError(
                _("Row %(row)s is missing %(field)s.")
                % {"row": row_number, "field": names[0]}
            )
        try:
            return float(value.replace(",", ""))
        except ValueError as exc:
            raise UserError(
                _("Row %(row)s has invalid number %(value)s for %(field)s.")
                % {"row": row_number, "value": value, "field": names[0]}
            ) from exc

    def _parse_date(self, value, row_number):
        if not value:
            return False
        try:
            parsed = fields.Date.to_date(value)
        except ValueError as exc:
            raise UserError(
                _("Row %(row)s has invalid invoice date %(value)s.")
                % {"row": row_number, "value": value}
            ) from exc
        if not parsed:
            raise UserError(
                _("Row %(row)s has invalid invoice date %(value)s.")
                % {"row": row_number, "value": value}
            )
        return parsed

    def _match_partner(self, row, row_number):
        Partner = self.env["res.partner"].with_context(active_test=False)
        customer_ref = self._first_value(row, "customer_ref", "partner_ref", "customer_id")
        customer_name = self._first_value(row, "customer_name", "partner_name", "customer")
        partner = Partner
        if customer_ref:
            partner = Partner.search([("ref", "=", customer_ref)], limit=1)
        if not partner and customer_name:
            partner = Partner.search([("name", "=", customer_name)], limit=1)
        if not partner:
            raise UserError(
                _("Row %(row)s customer was not found. Use customer_ref or customer_name.")
                % {"row": row_number}
            )
        return partner

    def _match_product(self, row, row_number):
        Product = self.env["product.product"].with_context(active_test=False)
        sku = self._first_value(row, "product_sku", "default_code", "sku")
        barcode = self._first_value(row, "barcode", "product_barcode")
        name = self._first_value(row, "product_name", "product")
        product = Product
        if sku:
            product = Product.search([("default_code", "=", sku)], limit=1)
        if not product and barcode:
            product = Product.search([("barcode", "=", barcode)], limit=1)
        if not product and name:
            product = Product.search([("name", "=", name)], limit=1)
        if not product:
            raise UserError(
                _("Row %(row)s product was not found. Use product_sku, barcode, or product_name.")
                % {"row": row_number}
            )
        return product

    def _match_refund_account(self, row, row_number):
        account_code = self._first_value(row, "refund_account_code", "account_code")
        if not account_code:
            return self.env["account.account"]
        account = self.env["account.account"].search([
            ("code", "=", account_code),
            ("company_ids", "in", [self.company_id.id]),
        ], limit=1)
        if not account:
            raise UserError(
                _("Row %(row)s credit account %(code)s was not found.")
                % {"row": row_number, "code": account_code}
            )
        return account

    def _match_taxes(self, row, row_number):
        tax_names = self._first_value(row, "odoo_tax_names", "tax_names")
        if not tax_names:
            return self.env["account.tax"]
        taxes = self.env["account.tax"]
        for tax_name in [name.strip() for name in tax_names.split("|") if name.strip()]:
            tax = self.env["account.tax"].search([
                ("name", "=", tax_name),
                ("type_tax_use", "=", "sale"),
                ("company_id", "=", self.company_id.id),
            ], limit=1)
            if not tax:
                raise UserError(
                    _("Row %(row)s tax %(tax)s was not found.")
                    % {"row": row_number, "tax": tax_name}
                )
            taxes |= tax
        return taxes

    def _match_or_create_lot(self, row, row_number, product):
        lot_name = self._first_value(row, "lot_serial", "serial_lot", "lot", "serial")
        if not lot_name:
            return self.env["stock.lot"]
        lot = self.env["stock.lot"].search([
            ("name", "=", lot_name),
            ("product_id", "=", product.id),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company_id.id),
        ], limit=1)
        if lot:
            return lot
        return self.env["stock.lot"].create({
            "name": lot_name,
            "product_id": product.id,
            "company_id": self.company_id.id,
        })
