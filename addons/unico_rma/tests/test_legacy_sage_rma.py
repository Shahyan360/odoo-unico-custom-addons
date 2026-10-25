import xml.etree.ElementTree as ET
import base64

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged

from .common import RmaTestCommon


@tagged("post_install", "-at_install")
class TestLegacySageRma(RmaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resolution_refund_with_return = cls.env["rma.resolution"].create({
            "rma_resolution": "Refund Legacy With Return",
            "rma_action": "refund_with_returned_item",
        })

        cls.stock_valuation_account = cls.env.company.account_stock_valuation_id
        if not cls.stock_valuation_account:
            cls.stock_valuation_account = cls._make_account(
                "LGRMA01", "Legacy RMA Stock Valuation", "asset_current"
            )
            cls.env.company.account_stock_valuation_id = cls.stock_valuation_account

        if not cls.stock_valuation_account.account_stock_variation_id:
            cls.stock_valuation_account.account_stock_variation_id = cls._make_account(
                "LGRMA02", "Legacy RMA Stock Variation", "expense"
            )

        if not cls.env.company.account_stock_journal_id:
            cls.env.company.account_stock_journal_id = cls.env["account.journal"].create({
                "name": "Legacy RMA Stock Journal",
                "code": "LRMA",
                "type": "general",
                "company_id": cls.env.company.id,
            })

        cls.legacy_refund_account = cls._make_account(
            "LGRMA03", "Legacy Sage Refunds", "income"
        )
        cls.legacy_clearing_account = cls._make_account(
            "LGRMA04", "Legacy Sage Inventory Clearing", "asset_current"
        )
        cls.legacy_source_location = cls.env.ref(
            "unico_rma.stock_location_legacy_sage_customer_returns"
        )
        cls.legacy_source_location.valuation_account_id = cls.legacy_clearing_account

        cls.legacy_category = cls.env["product.category"].create({
            "name": "Legacy RMA Valuated Products",
            "property_valuation": "real_time",
            "property_cost_method": "standard",
            "property_stock_valuation_account_id": cls.stock_valuation_account.id,
        })
        cls.legacy_product = cls.env["product.product"].create({
            "name": "Legacy RMA Storable Product",
            "type": "consu",
            "is_storable": True,
            "default_code": "LGRMA-PROD",
            "list_price": 120.0,
            "standard_price": 99.0,
            "categ_id": cls.legacy_category.id,
        })
        cls.legacy_replacement_product = cls.env["product.product"].create({
            "name": "Legacy RMA Replacement Product",
            "type": "consu",
            "is_storable": True,
            "default_code": "LGRMA-REPL",
            "list_price": 80.0,
            "standard_price": 55.0,
            "categ_id": cls.legacy_category.id,
        })
        cls.legacy_tax_group = cls.env["account.tax.group"].create({
            "name": "Legacy RMA Taxes",
            "company_id": cls.env.company.id,
        })
        cls.legacy_tax = cls.env["account.tax"].create({
            "name": "Legacy Tax 8%",
            "amount_type": "percent",
            "amount": 8.0,
            "type_tax_use": "sale",
            "company_id": cls.env.company.id,
            "tax_group_id": cls.legacy_tax_group.id,
        })
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1
        )
        cls.picking_type_in = cls.warehouse.in_type_id
        cls.stock_location = cls.warehouse.lot_stock_id

    @classmethod
    def _make_account(cls, code, name, account_type):
        return cls.env["account.account"].create({
            "code": code,
            "name": name,
            "account_type": account_type,
            "company_ids": [(6, 0, [cls.env.company.id])],
        })

    def _make_sale_manager_user(self, login="legacy_rma_manager"):
        return self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Legacy RMA Manager",
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("sales_team.group_sale_manager").id,
            ])],
        })

    def _make_legacy_rma(
        self,
        *,
        resolution=None,
        return_qty=2.0,
        legacy_max_return_qty=None,
        legacy_unit_cost=42.0,
        tax_treatment="no_odoo_tax",
        tax_ids=None,
        state="draft",
        entitlement=None,
        entitlement_qty_sold=None,
        entitlement_qty_returned=0.0,
        product=None,
        order_ref="SAGE-SO-001",
        invoice_ref="SAGE-INV-001",
        line_ref="SAGE-INV-001:1",
        duplicate_override=False,
    ):
        resolution = resolution or self.resolution_refund
        product = product or self.legacy_product
        if legacy_max_return_qty is None:
            legacy_max_return_qty = return_qty
        entitlement = entitlement or self.env["rma.legacy.entitlement"].create({
            "company_id": self.env.company.id,
            "partner_id": self.partner.id,
            "product_id": product.id,
            "sage_order_ref": order_ref,
            "sage_invoice_ref": invoice_ref,
            "sage_line_ref": line_ref,
            "invoice_date": "2025-11-15",
            "migration_cutoff_date": "2026-01-01",
            "qty_sold": entitlement_qty_sold or legacy_max_return_qty or return_qty,
            "qty_returned": entitlement_qty_returned,
            "price_unit": 120.0,
            "unit_cost": legacy_unit_cost,
            "refund_account_id": self.legacy_refund_account.id,
            "tax_ids": [(6, 0, tax_ids.ids)] if tax_ids else False,
        })
        line_vals = {
            "product_id": product.id,
            "legacy_entitlement_id": entitlement.id,
            "return_qty": return_qty,
            "price_unit": 120.0,
            "legacy_max_return_qty": legacy_max_return_qty,
            "legacy_unit_cost": legacy_unit_cost,
            "legacy_refund_account_id": self.legacy_refund_account.id,
            "rma_resolution_id": resolution.id,
            "return_reason_id": self.return_reason.id,
        }
        if tax_ids:
            line_vals["legacy_tax_ids"] = [(6, 0, tax_ids.ids)]

        return self.env["rma.main"].create({
            "rma_type": "legacy_sage",
            "subject": "Legacy Sage return",
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "legacy_sage_order_ref": order_ref,
            "legacy_sage_invoice_ref": invoice_ref,
            "legacy_sage_invoice_date": "2025-11-15",
            "legacy_duplicate_reference_override": duplicate_override,
            "legacy_tax_treatment": tax_treatment,
            "legacy_ar_treatment": "standalone_review",
            "legacy_picking_type_id": self.picking_type_in.id,
            "legacy_source_location_id": self.legacy_source_location.id,
            "legacy_return_location_id": self.stock_location.id,
            "rma_line_ids": [(0, 0, line_vals)],
            "state": state,
        })

    def test_legacy_rma_submits_without_sale_order(self):
        rma = self._make_legacy_rma()

        self.assertFalse(rma.sale_order)
        self.assertFalse(rma.delivery_order)
        self.assertEqual(rma.partner_id, self.partner)

        rma.action_submit()

        self.assertEqual(rma.state, "submit")

    def test_legacy_form_has_visible_required_customer_field(self):
        view = self.env.ref("unico_rma.view_rma_order_form")
        arch = ET.fromstring(view.arch_db)
        partner_fields = [
            field
            for field in arch.iter("field")
            if field.attrib.get("name") == "partner_id"
        ]

        self.assertTrue(any(
            field.attrib.get("required") == "rma_type == 'legacy_sage'"
            and field.attrib.get("invisible") == "rma_type != 'legacy_sage'"
            for field in partner_fields
        ))

    def test_legacy_form_hides_standard_partner_address_panels(self):
        view = self.env.ref("unico_rma.view_rma_order_form")
        arch = etree.fromstring(view.arch_db.encode())

        for field_name in ("del_partner", "inv_partner"):
            field = arch.xpath("//field[@name=$field_name]", field_name=field_name)[0]
            standard_partner_group = field.xpath("ancestor::group[1]")[0]
            self.assertEqual(
                standard_partner_group.get("invisible"),
                "rma_type == 'legacy_sage'",
            )

    def test_legacy_sage_line_quick_create_uses_compact_form(self):
        rma_view = self.env.ref("unico_rma.view_rma_order_form")
        rma_arch = etree.fromstring(rma_view.arch_db.encode())
        sage_line_fields = rma_arch.xpath("//field[@name='legacy_entitlement_id']")
        manager_fields = [
            field for field in sage_line_fields
            if field.get("groups") == "sales_team.group_sale_manager"
        ]
        clerk_fields = [
            field for field in sage_line_fields
            if field.get("groups") == "!sales_team.group_sale_manager"
        ]

        self.assertEqual(len(manager_fields), 2)
        self.assertEqual(len(clerk_fields), 2)
        self.assertTrue(all(
            "view_rma_legacy_entitlement_quick_form" in field.get("context", "")
            for field in manager_fields
        ))
        self.assertTrue(all(
            "default_qty_sold" not in field.get("context", "")
            for field in manager_fields
        ))
        self.assertTrue(all(
            "'no_create': True" in field.get("options", "")
            for field in clerk_fields
        ))

        quick_view = self.env.ref("unico_rma.view_rma_legacy_entitlement_quick_form")
        quick_arch = etree.fromstring(quick_view.arch_db.encode())

        self.assertEqual(quick_arch.get("string"), "Create Sage Line")
        self.assertFalse(quick_arch.xpath("//button[@name='action_view_rma_lines']"))
        self.assertFalse(quick_arch.xpath("//field[@name='rma_line_ids']"))
        self.assertFalse(quick_arch.xpath("//field[@name='imported_filename']"))
        self.assertTrue(quick_arch.xpath("//field[@name='partner_id'][@invisible='1']"))
        self.assertTrue(quick_arch.xpath("//field[@name='sage_line_ref']"))
        self.assertTrue(quick_arch.xpath("//field[@name='migration_cutoff_date']"))
        self.assertTrue(quick_arch.xpath("//field[@name='qty_sold']"))

    def test_legacy_form_entry_can_set_customer_and_submit(self):
        entitlement = self.env["rma.legacy.entitlement"].create({
            "company_id": self.env.company.id,
            "partner_id": self.partner.id,
            "product_id": self.legacy_product.id,
            "sage_order_ref": "SAGE-FORM-SO-001",
            "sage_invoice_ref": "SAGE-FORM-001",
            "sage_line_ref": "SAGE-FORM-001:1",
            "invoice_date": "2025-11-15",
            "migration_cutoff_date": "2026-01-01",
            "qty_sold": 1.0,
            "price_unit": 120.0,
            "unit_cost": 42.0,
            "refund_account_id": self.legacy_refund_account.id,
        })
        with Form(
            self.env["rma.main"].with_context(default_rma_type="legacy_sage"),
            view="unico_rma.view_rma_order_form",
        ) as rma_form:
            rma_form.rma_type = "legacy_sage"
            rma_form.subject = "Legacy Sage form return"
            rma_form.partner_id = self.partner
            rma_form.warehouse_id = self.warehouse
            rma_form.legacy_sage_invoice_ref = "SAGE-FORM-001"
            rma_form.legacy_picking_type_id = self.picking_type_in
            rma_form.legacy_source_location_id = self.legacy_source_location
            rma_form.legacy_return_location_id = self.stock_location
            with rma_form.rma_line_ids.new() as line_form:
                line_form.legacy_entitlement_id = entitlement
                line_form.product_id = self.legacy_product
                line_form.rma_resolution_id = self.resolution_refund
                line_form.return_reason_id = self.return_reason
                line_form.return_qty = 1.0
                line_form.legacy_max_return_qty = 1.0
                line_form.price_unit = 120.0
                line_form.legacy_unit_cost = 42.0
                line_form.legacy_refund_account_id = self.legacy_refund_account

        rma = rma_form.record

        self.assertEqual(rma.partner_id, self.partner)
        rma.action_submit()
        self.assertEqual(rma.state, "submit")

    def test_legacy_return_qty_cannot_exceed_entitlement_available_qty(self):
        rma = self._make_legacy_rma(
            return_qty=3.0,
            entitlement_qty_sold=2.0,
        )

        with self.assertRaises(UserError):
            rma.action_submit()

    def test_legacy_entitlement_consumes_across_multiple_rmas(self):
        entitlement = self.env["rma.legacy.entitlement"].create({
            "company_id": self.env.company.id,
            "partner_id": self.partner.id,
            "product_id": self.legacy_product.id,
            "sage_order_ref": "SAGE-SO-CUM",
            "sage_invoice_ref": "SAGE-INV-CUM",
            "sage_line_ref": "SAGE-INV-CUM:1",
            "invoice_date": "2025-11-15",
            "migration_cutoff_date": "2026-01-01",
            "qty_sold": 3.0,
            "price_unit": 120.0,
            "unit_cost": 42.0,
            "refund_account_id": self.legacy_refund_account.id,
        })
        first_rma = self._make_legacy_rma(
            entitlement=entitlement,
            return_qty=2.0,
            order_ref="SAGE-SO-CUM",
            invoice_ref="SAGE-INV-CUM",
            line_ref="SAGE-INV-CUM:1",
            state="submit",
        )
        first_rma.action_approve()
        self.assertEqual(entitlement.qty_returned, 2.0)
        self.assertEqual(entitlement.qty_available, 1.0)

        second_rma = self._make_legacy_rma(
            entitlement=entitlement,
            return_qty=2.0,
            order_ref="SAGE-SO-CUM",
            invoice_ref="SAGE-INV-CUM",
            line_ref="SAGE-INV-CUM:1",
            duplicate_override=True,
        )
        with self.assertRaises(UserError):
            second_rma.action_submit()

    def test_manager_created_sage_line_still_enforces_qty_sold(self):
        manager = self._make_sale_manager_user("legacy_rma_gap_manager")
        entitlement = self.env["rma.legacy.entitlement"].with_user(manager).create({
            "company_id": self.env.company.id,
            "partner_id": self.partner.id,
            "product_id": self.legacy_product.id,
            "sage_order_ref": "SAGE-SO-MANUAL",
            "sage_invoice_ref": "SAGE-INV-MANUAL",
            "sage_line_ref": "SAGE-INV-MANUAL:1",
            "invoice_date": "2025-11-15",
            "migration_cutoff_date": "2026-01-01",
            "qty_sold": 1.0,
            "price_unit": 120.0,
            "unit_cost": 42.0,
            "refund_account_id": self.legacy_refund_account.id,
        })
        rma = self._make_legacy_rma(
            entitlement=entitlement,
            return_qty=2.0,
            order_ref="SAGE-SO-MANUAL",
            invoice_ref="SAGE-INV-MANUAL",
            line_ref="SAGE-INV-MANUAL:1",
        )

        with self.assertRaises(UserError):
            rma.action_submit()

    def test_legacy_reject_releases_consumed_entitlement(self):
        rma = self._make_legacy_rma(
            return_qty=2.0,
            entitlement_qty_sold=2.0,
            state="submit",
            order_ref="SAGE-SO-REJECT",
            invoice_ref="SAGE-INV-REJECT",
            line_ref="SAGE-INV-REJECT:1",
        )
        entitlement = rma.rma_line_ids.legacy_entitlement_id
        rma.action_approve()
        self.assertEqual(entitlement.qty_returned, 2.0)

        self.env["rma.reject.wizard"].create({
            "rma_id": rma.id,
            "reject_reason": "Customer cancelled",
        }).action_confirm()

        self.assertEqual(rma.state, "reject")
        self.assertEqual(entitlement.qty_returned, 0.0)
        self.assertEqual(rma.rma_line_ids.legacy_entitlement_consumed_qty, 0.0)

    def test_legacy_entitlement_direct_mutation_is_manager_only(self):
        entitlement = self._make_legacy_rma().rma_line_ids.legacy_entitlement_id
        clerk = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Legacy RMA Clerk",
            "login": "legacy_rma_clerk",
            "email": "legacy_rma_clerk@example.com",
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })

        self.assertEqual(entitlement.with_user(clerk).qty_available, 2.0)
        with self.assertRaises(AccessError):
            entitlement.with_user(clerk).write({"qty_returned": 1.0})

        with self.assertRaises(AccessError):
            self.env["rma.legacy.entitlement"].with_user(clerk).create({
                "company_id": self.env.company.id,
                "partner_id": self.partner.id,
                "product_id": self.legacy_product.id,
                "sage_line_ref": "BLOCKED-CLERK:1",
                "migration_cutoff_date": "2026-01-01",
                "qty_sold": 1.0,
            })

        with self.assertRaises(AccessError):
            self.env["rma.legacy.entitlement.import"].with_user(clerk).create({
                "file_data": base64.b64encode(b"qty_sold\n1"),
                "filename": "blocked.csv",
                "migration_cutoff_date": "2026-01-01",
            })

    def test_legacy_credit_note_uses_explicit_account_and_no_odoo_tax(self):
        rma = self._make_legacy_rma(
            tax_ids=self.legacy_tax,
            tax_treatment="no_odoo_tax",
            state="submit",
        )

        rma.action_approve()

        credit_note = self.env["account.move"].search([
            ("rma_id", "=", rma.id),
            ("move_type", "=", "out_refund"),
        ])
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.state, "draft")
        self.assertFalse(credit_note.sale_id)
        self.assertEqual(credit_note.invoice_origin, "SAGE-INV-001")

        cn_line = credit_note.invoice_line_ids.filtered(
            lambda line: line.rma_line_id == rma.rma_line_ids
        )
        self.assertEqual(cn_line.account_id, self.legacy_refund_account)
        self.assertFalse(cn_line.tax_ids)
        self.assertEqual(cn_line.quantity, 2.0)
        self.assertIn("Tax handling: Do Not Add Odoo Tax", credit_note.narration)
        self.assertIn("Credit handling: Create Credit for Review", credit_note.narration)

    def test_legacy_credit_note_requires_accounting_approval_to_post(self):
        rma = self._make_legacy_rma(state="submit")
        rma.action_approve()
        credit_note = self.env["account.move"].search([
            ("rma_id", "=", rma.id),
            ("move_type", "=", "out_refund"),
        ])

        with self.assertRaises(UserError):
            credit_note.action_post()

        rma.legacy_accounting_approved = True
        credit_note.action_post()
        self.assertEqual(credit_note.state, "posted")

    def test_legacy_duplicate_sage_reference_requires_manager_override(self):
        first_rma = self._make_legacy_rma(
            order_ref="SAGE-SO-DUP",
            invoice_ref="SAGE-INV-DUP",
            line_ref="SAGE-INV-DUP:1",
        )
        first_rma.action_submit()

        second_rma = self._make_legacy_rma(
            order_ref="SAGE-SO-DUP",
            invoice_ref="SAGE-INV-DUP",
            line_ref="SAGE-INV-DUP:2",
            return_qty=1.0,
        )
        with self.assertRaises(UserError):
            second_rma.action_submit()

        second_rma.legacy_duplicate_reference_override = True
        second_rma.action_submit()
        self.assertEqual(second_rma.state, "submit")

    def test_legacy_entitlement_import_creates_rows_with_cutoff(self):
        self.partner.ref = "CUST-RMA"
        csv_content = "\n".join([
            "sage_invoice_ref,line_number,customer_ref,product_sku,qty_sold,qty_returned,unit_price,unit_cost,refund_account_code,tax_code",
            "SAGE-CSV-001,1,CUST-RMA,LGRMA-PROD,4,1,120,42,LGRMA03,TX8",
        ])
        wizard = self.env["rma.legacy.entitlement.import"].create({
            "file_data": base64.b64encode(csv_content.encode()),
            "filename": "legacy_entitlements.csv",
            "migration_cutoff_date": "2026-01-01",
        })

        wizard.action_import()

        entitlement = self.env["rma.legacy.entitlement"].search([
            ("sage_line_ref", "=", "SAGE-CSV-001:1"),
        ])
        self.assertEqual(len(entitlement), 1)
        self.assertEqual(entitlement.partner_id, self.partner)
        self.assertEqual(entitlement.product_id, self.legacy_product)
        self.assertEqual(entitlement.qty_sold, 4.0)
        self.assertEqual(entitlement.qty_returned, 1.0)
        self.assertEqual(entitlement.qty_available, 3.0)
        self.assertEqual(entitlement.migration_cutoff_date.isoformat(), "2026-01-01")

    def test_legacy_receipt_values_partial_return_at_sage_cost_and_clearing(self):
        rma = self._make_legacy_rma(
            resolution=self.resolution_refund_with_return,
            return_qty=5.0,
            legacy_unit_cost=10.0,
            state="submit",
        )

        rma.action_approve()

        picking = self.env["stock.picking"].search([("rma_id", "=", rma.id)])
        self.assertEqual(len(picking), 1)
        move = picking.move_ids
        self.assertEqual(move.value_manual, 50.0)

        move.quantity = 2.0
        move.picked = True
        backorder_action = picking.button_validate()
        if isinstance(backorder_action, dict):
            backorder_wizard = self.env[backorder_action["res_model"]].with_context(
                backorder_action["context"]
            ).create({})
            backorder_wizard.process_cancel_backorder()

        self.assertEqual(move.state, "done")
        self.assertEqual(rma.state, "processing")
        self.assertEqual(move.value_manual, 20.0)
        self.assertEqual(move.value, 20.0)

        credit_line = move.account_move_id.line_ids.filtered(
            lambda line: line.account_id == self.legacy_clearing_account
        )
        debit_line = move.account_move_id.line_ids.filtered(
            lambda line: line.account_id == self.stock_valuation_account
        )
        self.assertEqual(credit_line.credit, 20.0)
        self.assertEqual(debit_line.debit, 20.0)

        credit_note = self.env["account.move"].search([
            ("rma_id", "=", rma.id),
            ("move_type", "=", "out_refund"),
        ])
        cn_line = credit_note.invoice_line_ids.filtered(
            lambda line: line.rma_line_id == rma.rma_line_ids
        )
        self.assertEqual(rma.rma_line_ids.return_qty, 2.0)
        self.assertEqual(cn_line.quantity, 2.0)

    def test_legacy_return_requires_configured_source_clearing_account(self):
        source_location = self.legacy_source_location.copy({
            "name": "Legacy Source Without Clearing",
            "valuation_account_id": False,
        })
        rma = self._make_legacy_rma(
            resolution=self.resolution_refund_with_return,
            state="submit",
        )
        rma.legacy_source_location_id = source_location

        with self.assertRaises(UserError):
            rma.action_approve()

    def test_legacy_replacement_creates_confirmed_replacement_sale_order(self):
        rma = self._make_legacy_rma(
            resolution=self.resolution_replacement,
            return_qty=0.0,
            legacy_max_return_qty=1.0,
            entitlement_qty_sold=1.0,
            state="submit",
            order_ref="SAGE-SO-REPL",
            invoice_ref="SAGE-INV-REPL",
            line_ref="SAGE-INV-REPL:1",
        )
        rma.legacy_replacement_pricing_policy = "zero_warranty"
        self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.legacy_replacement_product.id,
            "qty": 1,
        })

        rma.action_approve()

        sale_order = self.env["sale.order"].search([("origin", "=", rma.name)])
        self.assertEqual(len(sale_order), 1)
        self.assertEqual(sale_order.state, "sale")
        self.assertEqual(sale_order.rma_id, rma)
        self.assertEqual(sale_order.order_line.product_id, self.legacy_replacement_product)
        self.assertEqual(sale_order.order_line.price_unit, 0.0)
        if "unico_purchase_note" in sale_order.order_line._fields:
            self.assertIn(rma.name, sale_order.order_line.unico_purchase_note)
            self.assertIn("No-Charge Warranty", sale_order.order_line.unico_purchase_note)
        self.assertTrue(sale_order.picking_ids)

    def test_legacy_billable_replacement_uses_customer_pricelist(self):
        pricelist = self.env["product.pricelist"].create({
            "name": "Legacy Replacement 20 Percent Off",
            "currency_id": self.env.company.currency_id.id,
        })
        self.env["product.pricelist.item"].create({
            "pricelist_id": pricelist.id,
            "applied_on": "3_global",
            "compute_price": "percentage",
            "percent_price": 20,
        })
        self.partner.property_product_pricelist = pricelist
        rma = self._make_legacy_rma(
            resolution=self.resolution_replacement,
            return_qty=0.0,
            legacy_max_return_qty=1.0,
            entitlement_qty_sold=1.0,
            state="submit",
            order_ref="SAGE-SO-BILLREPL",
            invoice_ref="SAGE-INV-BILLREPL",
            line_ref="SAGE-INV-BILLREPL:1",
        )
        rma.legacy_replacement_pricing_policy = "billable"
        self.env["rma.replace.order"].create({
            "rma_id": rma.id,
            "product_id": self.legacy_replacement_product.id,
            "qty": 1,
        })

        rma.action_approve()

        sale_order = self.env["sale.order"].search([("rma_id", "=", rma.id)])
        self.assertEqual(sale_order.pricelist_id, pricelist)
        self.assertEqual(sale_order.order_line.price_unit, 64.0)
        if "unico_purchase_note" in sale_order.order_line._fields:
            self.assertIn(rma.name, sale_order.order_line.unico_purchase_note)
            self.assertIn("Bill Customer", sale_order.order_line.unico_purchase_note)

    def test_legacy_replacement_discount_must_be_percentage(self):
        rma = self._make_legacy_rma()

        with self.assertRaises(ValidationError):
            rma.legacy_replacement_discount = 101.0

    def test_legacy_lot_tracked_return_requires_and_propagates_lot(self):
        lot_product = self.env["product.product"].create({
            "name": "Legacy Lot Product",
            "type": "consu",
            "is_storable": True,
            "tracking": "lot",
            "list_price": 90.0,
            "standard_price": 40.0,
            "categ_id": self.legacy_category.id,
        })
        lot = self.env["stock.lot"].create({
            "name": "LOT-LEGACY-1",
            "product_id": lot_product.id,
            "company_id": self.env.company.id,
        })
        rma = self._make_legacy_rma(
            product=lot_product,
            resolution=self.resolution_refund_with_return,
            return_qty=1.0,
            entitlement_qty_sold=1.0,
            state="submit",
            order_ref="SAGE-SO-LOT",
            invoice_ref="SAGE-INV-LOT",
            line_ref="SAGE-INV-LOT:1",
        )
        rma.rma_line_ids.lot_ids = [(5, 0, 0)]

        with self.assertRaises(UserError):
            rma.action_approve()

        rma.rma_line_ids.lot_ids = [(6, 0, lot.ids)]
        rma.action_approve()

        picking = self.env["stock.picking"].search([("rma_id", "=", rma.id)])
        self.assertEqual(picking.move_ids.lot_ids, lot)
