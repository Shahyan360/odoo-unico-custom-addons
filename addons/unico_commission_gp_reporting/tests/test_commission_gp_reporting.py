# -*- coding: utf-8 -*-

from datetime import date

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.unico_commission_gp_reporting.models.commission_gp import PUBLISHED_STATES


@tagged('post_install', '-at_install')
class TestUnicoCommissionGpReporting(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'unico_commission_gp_reporting.group_unico_commission_gp_manager'
        )
        cls.partner = cls.env['res.partner'].create({
            'name': 'Commission Customer',
            'user_id': cls.env.user.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Commission Test Door',
            'type': 'consu',
            'invoice_policy': 'order',
            'list_price': 100.0,
            'standard_price': 60.0,
        })
        cls.excluded_product = cls.env['product.product'].create({
            'name': 'Excluded Commission Product',
            'type': 'consu',
            'invoice_policy': 'order',
            'list_price': 50.0,
            'standard_price': 20.0,
        })
        cls.excluded_product.product_tmpl_id.unico_commission_gp_excluded = True

    def _create_posted_invoice_from_sale(
        self,
        product=None,
        quantity=2.0,
        price_unit=100.0,
        purchase_price=None,
        pricelist=None,
        salesperson=None,
        invoice_date=date(2026, 5, 15),
        paid=True,
        payment_date=False,
    ):
        # Account-owner attribution now wins over this parameter when the customer has an owner.
        product = product or self.product
        order_vals = {
            'partner_id': self.partner.id,
            'user_id': (salesperson or self.env.user).id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': quantity,
                'price_unit': price_unit,
            })],
        }
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        order = self.env['sale.order'].create(order_vals)
        order.action_confirm()
        sale_line = order.order_line.filtered(lambda line: not line.display_type)[:1]
        sale_line.purchase_price = product.standard_price if purchase_price is None else purchase_price
        invoice = order._create_invoices()
        invoice.invoice_date = invoice_date
        invoice.action_post()
        if paid:
            self._pay_invoice(invoice, payment_date=payment_date or invoice_date)
        return order, sale_line, invoice

    def _pay_invoice(self, invoice, payment_date=False, amount=False):
        values = {
            'payment_date': payment_date or invoice.invoice_date or invoice.date,
        }
        if amount is not False:
            values['amount'] = amount
        self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=invoice.ids,
        ).create(values)._create_payments()
        invoice.invalidate_recordset([
            'payment_state',
            'unico_commission_cash_collected',
            'unico_commission_collection_date',
        ])
        return invoice

    def _create_period(self, salesperson=False, date_start=date(2026, 5, 1), date_end=date(2026, 5, 31)):
        return self.env['unico.commission.gp.period'].create({
            'date_start': date_start,
            'date_end': date_end,
            'salesperson_id': salesperson and salesperson.id or False,
            'company_id': self.env.company.id,
        })

    def _create_posted_invoice_at_margin(self, margin_percent, price_unit=100.0):
        return self._create_posted_invoice_from_sale(
            quantity=1.0,
            price_unit=price_unit,
            purchase_price=price_unit * (1.0 - margin_percent),
        )

    def _create_commission_user(self, login, name='Commission User'):
        return self.env['res.users'].with_context(no_reset_password=True).create({
            'name': name,
            'login': login,
            'email': '%s@example.com' % login,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('sales_team.group_sale_salesman').id,
                self.env.ref('unico_commission_gp_reporting.group_unico_commission_gp_user').id,
            ])],
        })

    def _create_commission_manager(self, login, name='Commission Manager'):
        return self.env['res.users'].with_context(no_reset_password=True).create({
            'name': name,
            'login': login,
            'email': '%s@example.com' % login,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('sales_team.group_sale_salesman_all_leads').id,
                self.env.ref('account.group_account_readonly').id,
                self.env.ref('unico_commission_gp_reporting.group_unico_commission_gp_manager').id,
            ])],
        })

    def test_product_manager_can_duplicate_product_and_keeps_exclusion(self):
        """Duplicate must not require Commission GP access, nor lose the flag."""
        product_manager = self.env['res.users'].create({
            'name': 'Plain Product Manager',
            'login': 'plain_product_manager',
            'email': 'plain.product.manager@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('product.group_product_manager').id,
            ])],
        })
        self.assertFalse(product_manager.has_group(
            'unico_commission_gp_reporting.group_unico_commission_gp_manager'
        ))
        template = self.excluded_product.product_tmpl_id
        template.unico_commission_gp_exclusion_note = 'Freight pass-through'

        # Duplicating used to raise AccessError here: copy_data reads every
        # copyable field with the caller's own rights.
        copied = template.with_user(product_manager).copy({
            'name': 'Duplicated Excluded Commission Product',
        })

        self.assertTrue(copied.exists())
        self.assertTrue(copied.sudo().unico_commission_gp_excluded)
        self.assertEqual(
            copied.sudo().unico_commission_gp_exclusion_note,
            'Freight pass-through',
        )

    def test_product_duplicate_default_overrides_carried_exclusion(self):
        """An explicit default still wins over the elevated carry-over."""
        template = self.excluded_product.product_tmpl_id

        copied = template.copy({
            'name': 'Reclassified Commission Product',
            'unico_commission_gp_excluded': False,
        })

        self.assertFalse(copied.unico_commission_gp_excluded)

    def test_commission_plan_rejects_unordered_thresholds(self):
        with self.assertRaises(ValidationError):
            self.env['commission.plan'].create({
                'name': 'Bad Plan',
                'margin_full_payout_threshold': 0.20,
                'margin_reduced_75_threshold': 0.25,
                'margin_reduced_50_threshold': 0.28,
            })

    def test_commission_plan_accepts_valid_policy(self):
        plan = self.env['commission.plan'].create({
            'name': 'Valid Plan',
            'margin_full_payout_threshold': 0.30,
            'margin_reduced_75_threshold': 0.25,
            'margin_reduced_50_threshold': 0.20,
            'margin_full_payout_factor': 1.0,
            'margin_reduced_75_payout_factor': 0.75,
            'margin_reduced_50_payout_factor': 0.50,
            'default_commission_rate': 0.05,
            'require_cash_collected': True,
            'attribution_basis': 'account_owner',
        })
        self.assertTrue(plan.active)
        self.assertEqual(plan.attribution_basis, 'account_owner')

    def test_commission_plan_rejects_rate_above_100_percent(self):
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            plan.default_commission_rate = 1.5

    def test_period_gp_eligible_rejects_zero_percent(self):
        period = self._create_period()

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            period.write({'margin_full_payout_factor': 0.0})

    def test_period_snapshots_plan_policy_at_calculate(self):
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')
        plan.default_commission_rate = 0.05
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        self.assertEqual(period.plan_id, plan)

        period.action_calculate()
        self.assertAlmostEqual(period.margin_full_payout_threshold, 0.28, places=2)
        self.assertAlmostEqual(period.margin_full_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(period.default_commission_rate, 0.05, places=2)
        self.assertEqual(period.require_cash_collected, plan.require_cash_collected)
        self.assertEqual(period.attribution_basis, plan.attribution_basis)

    def test_old_period_keeps_snapshot_after_plan_change(self):
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')
        plan.default_commission_rate = 0.05
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()

        plan.default_commission_rate = 0.09
        self.assertAlmostEqual(period.default_commission_rate, 0.05, places=2)

    def test_attribution_basis_sales_order_user_credits_order_rep(self):
        order_rep = self._create_commission_user('order_rep_basis', 'Order Rep Basis')
        account_owner = self._create_commission_user('acct_owner_basis', 'Acct Owner Basis')
        partner = self.env['res.partner'].create({'name': 'SO Basis Co', 'user_id': account_owner.id})
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')
        plan.attribution_basis = 'sales_order_user'
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'user_id': order_rep.id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': 1.0, 'price_unit': 100.0})],
        })
        order.with_context(skip_unico_commission_salesperson_sync=True).write({'user_id': order_rep.id})
        order.action_confirm()
        sale_line = order.order_line.filtered(lambda l: not l.display_type)[:1]
        sale_line.purchase_price = 60.0
        invoice = order._create_invoices()
        invoice.invoice_date = date(2026, 5, 15)
        invoice.action_post()
        self._pay_invoice(invoice, payment_date=date(2026, 5, 15))

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertEqual(line.salesperson_id, order_rep)
        self.assertEqual(line.attribution_source, 'sales_order_user')

    def test_attribution_basis_account_owner_unchanged(self):
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')
        plan.attribution_basis = 'account_owner'
        sarah = self._create_commission_user('sarah_basis', 'Sarah Basis')
        self.partner.user_id = sarah
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertEqual(line.salesperson_id, sarah)
        self.assertEqual(line.attribution_source, 'sale_order_snapshot')

    def test_require_cash_collected_false_makes_unpaid_invoice_commissionable(self):
        plan = self.env.ref('unico_commission_gp_reporting.commission_plan_standard_gp')
        plan.require_cash_collected = False
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(
            paid=False, purchase_price=60.0,
        )
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')

    def test_period_payroll_lifecycle_transitions(self):
        self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        self.assertTrue(period.last_calculated_date)

        period.action_submit_for_review()
        self.assertEqual(period.state, 'ready_for_review')

        period.action_approve()
        self.assertEqual(period.state, 'approved')

        period.action_export_payroll()
        self.assertEqual(period.state, 'exported')
        self.assertTrue(period.exported_date)
        self.assertEqual(period.exported_by_id, self.env.user)

    def test_uncalculated_period_cannot_enter_payroll_lifecycle(self):
        period = self._create_period()

        with self.assertRaises(UserError):
            period.action_submit_for_review()

        period.state = 'ready_for_review'
        with self.assertRaises(UserError):
            period.action_approve()

        period.state = 'approved'
        with self.assertRaises(UserError):
            period.action_export_payroll()

    def test_manager_can_recalculate_ready_period_to_draft(self):
        self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        period.action_submit_for_review()

        period.action_calculate()

        self.assertEqual(period.state, 'draft')
        self.assertEqual(period.source_line_count, 1)

    def test_manager_can_reopen_ready_period(self):
        manager = self._create_commission_manager('ready_reopen_manager', 'Ready Reopen Manager')
        self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        period.action_submit_for_review()

        period.with_user(manager).action_reopen()

        self.assertEqual(period.state, 'draft')

    def test_non_admin_manager_cannot_reopen_approved_period(self):
        manager = self._create_commission_manager('approved_reopen_manager', 'Approved Reopen Manager')
        self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        period.action_submit_for_review()
        period.action_approve()

        with self.assertRaises(AccessError):
            period.with_user(manager).action_reopen()

    def test_legacy_closed_period_state_maps_to_approved(self):
        period = self._create_period()
        period._write({'state': 'closed'})
        period.invalidate_recordset(['state'])

        self.env['unico.commission.gp.period'].init()
        period.invalidate_recordset(['state'])

        self.assertEqual(period.state, 'approved')

    def test_exported_period_cannot_be_reopened(self):
        self._create_posted_invoice_from_sale(quantity=1.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        period.action_submit_for_review()
        period.action_approve()
        period.action_export_payroll()
        with self.assertRaises(UserError):
            period.action_reopen()

    def test_export_payroll_csv_contains_salesperson_totals(self):
        import base64
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.05
        period.action_calculate()
        period.action_submit_for_review()
        period.action_approve()

        action = period.action_export_payroll()
        attachment = self.env['ir.attachment'].browse(action['res_id'])
        content = base64.b64decode(attachment.datas).decode('utf-8')
        self.assertIn('Salesperson,Commission Basis,Rate,Calculated,Adjustment,Final Payable', content)
        self.assertIn(self.env.user.name, content)
        self.assertIn('TOTAL', content)

    def test_pending_collection_aggregate(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0, paid=False)
        period = self._create_period()
        period.action_calculate()
        self.assertEqual(period.pending_collection_line_count, 1)
        self.assertAlmostEqual(period.pending_collection_amount, 40.0, places=2)

    def test_posted_invoice_creates_commissionable_gp_line(self):
        order, sale_line, invoice = self._create_posted_invoice_from_sale()

        period = self._create_period()
        period.action_calculate()

        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.salesperson_id, self.env.user)
        self.assertEqual(line.sale_order_id, order)
        self.assertEqual(line.sale_order_line_id, sale_line)
        self.assertEqual(line.invoice_payment_state, 'paid')
        self.assertEqual(line.collection_date, date(2026, 5, 15))
        self.assertTrue(line.cash_collected)
        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertAlmostEqual(line.revenue_amount, 200.0, places=2)
        self.assertAlmostEqual(line.cost_amount, 120.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 80.0, places=2)
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 80.0, places=2)
        self.assertAlmostEqual(period.commissionable_gross_profit_amount, 80.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 80.0, places=2)
        self.assertEqual(period.full_payout_line_count, 1)

    def test_unpaid_invoice_is_pending_collection_not_commissionable(self):
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(paid=False)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(line), 1)
        self.assertEqual(line.invoice_payment_state, 'not_paid')
        self.assertFalse(line.cash_collected)
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'pending_collection')
        self.assertAlmostEqual(line.revenue_amount, 200.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 80.0, places=2)
        self.assertAlmostEqual(line.commission_basis_amount, 0.0, places=2)
        self.assertAlmostEqual(period.commissionable_gross_profit_amount, 0.0, places=2)

    def test_partial_payment_remains_pending_collection(self):
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(paid=False)
        self._pay_invoice(invoice, amount=invoice.amount_total / 2.0)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(line), 1)
        self.assertEqual(line.invoice_payment_state, 'partial')
        self.assertFalse(line.cash_collected)
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'pending_collection')

    def test_paid_invoice_is_commissionable_in_collection_period(self):
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(
            invoice_date=date(2026, 5, 15),
            payment_date=date(2026, 6, 3),
        )

        may_period = self._create_period()
        may_period.action_calculate()
        may_line = may_period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertEqual(len(may_line), 1)
        self.assertEqual(may_line.collection_date, date(2026, 6, 3))
        self.assertFalse(may_line.cash_collected)
        self.assertFalse(may_line.commissionable)
        self.assertEqual(may_line.effective_exclusion_reason, 'pending_collection')

        june_period = self._create_period(date_start=date(2026, 6, 1), date_end=date(2026, 6, 30))
        june_period.action_calculate()
        june_line = june_period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(june_line), 1)
        self.assertEqual(june_line.invoice_date, date(2026, 5, 15))
        self.assertEqual(june_line.collection_date, date(2026, 6, 3))
        self.assertTrue(june_line.cash_collected)
        self.assertTrue(june_line.commissionable)
        self.assertEqual(june_line.effective_exclusion_reason, 'none')
        self.assertAlmostEqual(june_period.margin_adjusted_commissionable_gp_amount, 80.0, places=2)
        self.assertAlmostEqual(
            may_period.margin_adjusted_commissionable_gp_amount
            + june_period.margin_adjusted_commissionable_gp_amount,
            80.0,
            places=2,
        )

    def test_credit_note_reconciliation_without_cash_does_not_make_invoice_commissionable(self):
        __order, sale_line, invoice = self._create_posted_invoice_from_sale(
            quantity=1.0,
            paid=False,
        )
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'invoice_user_id': self.env.user.id,
            'invoice_date': date(2026, 5, 20),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'sale_line_ids': [(6, 0, sale_line.ids)],
            })],
        })
        refund.action_post()
        (invoice + refund).line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable'
        ).reconcile()
        invoice.invalidate_recordset([
            'payment_state',
            'unico_commission_cash_collected',
            'unico_commission_collection_date',
        ])
        refund.invalidate_recordset([
            'payment_state',
            'unico_commission_cash_collected',
            'unico_commission_collection_date',
        ])

        period = self._create_period()
        period.action_calculate()
        invoice_line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        refund_line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == refund)

        self.assertEqual(invoice.payment_state, 'reversed')
        self.assertFalse(invoice.unico_commission_cash_collected)
        self.assertFalse(refund.unico_commission_cash_collected)
        self.assertFalse(invoice_line.commissionable)
        self.assertFalse(refund_line.commissionable)
        self.assertEqual(invoice_line.effective_exclusion_reason, 'pending_collection')
        self.assertEqual(refund_line.effective_exclusion_reason, 'pending_collection')
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 0.0, places=2)

    def test_sales_order_and_commission_use_commercial_account_owner(self):
        sarah = self._create_commission_user('sarah_account_owner', 'Sarah Account Owner')
        account = self.env['res.partner'].create({
            'name': 'ABC Construction',
            'user_id': sarah.id,
        })
        contact = self.env['res.partner'].create({
            'name': 'ABC Job Contact',
            'parent_id': account.id,
            'type': 'delivery',
        })

        order = self.env['sale.order'].create({
            'partner_id': contact.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1.0,
                'price_unit': 100.0,
            })],
        })
        self.assertEqual(order.user_id, sarah)
        order.action_confirm()
        sale_line = order.order_line.filtered(lambda line: not line.display_type)[:1]
        sale_line.purchase_price = 60.0
        invoice = order._create_invoices()
        invoice.invoice_date = date(2026, 5, 15)
        invoice.action_post()

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(line.salesperson_id, sarah)
        self.assertEqual(line.account_salesperson_id, sarah)
        self.assertEqual(line.attribution_partner_id, account)
        self.assertEqual(line.attribution_source, 'sale_order_snapshot')

    def test_explicit_sales_order_salesperson_is_not_overwritten_on_create(self):
        explicit_salesperson = self._create_commission_user('explicit_salesperson', 'Explicit Salesperson')
        account_owner = self._create_commission_user('account_owner_not_forced', 'Account Owner Not Forced')
        partner = self.env['res.partner'].create({
            'name': 'Explicit Salesperson Customer',
            'user_id': account_owner.id,
        })

        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'user_id': explicit_salesperson.id,
        })

        self.assertEqual(order.user_id, explicit_salesperson)

    def test_own_document_salesperson_keeps_quote_access_for_account_owned_by_another_user(self):
        quote_user = self._create_commission_user('quote_entry_user', 'Quote Entry User')
        account_owner = self._create_commission_user('quote_account_owner', 'Quote Account Owner')
        partner = self.env['res.partner'].create({
            'name': 'Account-Owned Quote Customer',
            'user_id': account_owner.id,
        })

        order = self.env['sale.order'].with_user(quote_user).create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1.0,
                'price_unit': 100.0,
            })],
        })
        line = order.order_line.filtered(lambda sale_line: not sale_line.display_type)[:1]

        self.assertEqual(order.user_id, quote_user)
        self.assertEqual(line.salesman_id, quote_user)
        line.with_user(quote_user).write({'product_uom_qty': 2.0})

        order.with_user(quote_user).action_confirm()
        self.assertEqual(order.user_id, quote_user)
        self.assertEqual(order.unico_commission_account_salesperson_id, account_owner)
        self.assertEqual(order.unico_commission_attribution_source, 'account_owner')

    def test_commission_uses_snapshot_when_account_owner_changes_later(self):
        original_owner = self._create_commission_user('original_owner', 'Original Owner')
        new_owner = self._create_commission_user('new_owner', 'New Owner')
        self.partner.user_id = original_owner
        order, __sale_line, invoice = self._create_posted_invoice_from_sale(
            salesperson=new_owner,
            invoice_date=date(2026, 5, 15),
        )
        self.partner.user_id = new_owner

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(line.salesperson_id, original_owner)
        self.assertEqual(order.unico_commission_account_salesperson_id, original_owner)
        self.assertEqual(line.attribution_warning, 'account_salesperson_changed')

    def test_missing_account_salesperson_is_exception(self):
        self.partner.user_id = False
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale()

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertFalse(line.salesperson_id)
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'missing_account_salesperson')

    def test_sales_order_keeps_existing_salesperson_when_account_has_no_owner(self):
        order_entry_user = self._create_commission_user('order_entry_no_owner', 'Order Entry No Owner')
        no_owner_partner = self.env['res.partner'].create({'name': 'No Owner Account'})

        order = self.env['sale.order'].create({
            'partner_id': no_owner_partner.id,
            'user_id': order_entry_user.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1.0,
                'price_unit': 100.0,
            })],
        })

        self.assertEqual(order.user_id, order_entry_user)

    def test_period_builds_salesperson_commission_summary(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.05

        period.action_calculate()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary.commission_basis_amount, 40.0, places=2)
        self.assertAlmostEqual(summary.calculated_commission_amount, 2.0, places=2)
        self.assertAlmostEqual(summary.final_approved_commission_amount, 2.0, places=2)
        self.assertAlmostEqual(period.team_commission_total, 2.0, places=2)

    def test_manager_can_recalculate_draft_period_without_general_line_unlink_access(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        manager = self._create_commission_manager('recalculate_manager', 'Recalculate Manager')
        period = self._create_period()
        period.with_user(manager).action_calculate()
        first_line = period.line_ids[:1]

        with self.assertRaises(AccessError):
            first_line.with_user(manager).check_access('unlink')

        period.with_user(manager).action_calculate()

        self.assertEqual(len(period.line_ids), 1)
        self.assertNotEqual(period.line_ids.id, first_line.id)
        self.assertAlmostEqual(period.line_ids.gross_profit_amount, 40.0, places=2)

    def test_manager_recalculation_removes_obsolete_summary_without_general_unlink_access(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        manager = self._create_commission_manager('obsolete_summary_manager', 'Obsolete Summary Manager')
        other_user = self._create_commission_user('obsolete_summary_owner', 'Obsolete Summary Owner')
        period = self._create_period()
        period.with_user(manager).action_calculate()
        first_summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        with self.assertRaises(AccessError):
            first_summary.with_user(manager).check_access('unlink')

        period.with_user(manager).write({'salesperson_id': other_user.id})
        period.with_user(manager).action_calculate()

        self.assertFalse(period.line_ids)
        self.assertFalse(period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user))

    def test_summary_rate_drives_line_and_period_commission_amounts(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.05
        period.action_calculate()
        line = period.line_ids[:1]
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        summary.write({'commission_rate': 0.07})

        self.assertAlmostEqual(line.commission_rate, 0.07, places=2)
        self.assertAlmostEqual(line.calculated_commission_amount, 2.8, places=2)
        self.assertAlmostEqual(summary.calculated_commission_amount, 2.8, places=2)
        self.assertAlmostEqual(period.team_commission_total, 2.8, places=2)

    def test_summary_rate_rejects_rate_above_100_percent(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            summary.write({'commission_rate': 1.5})

    def test_summary_reduced_band_gp_eligible_drives_line_and_period_basis(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.25)
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.10
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        summary.write({'margin_reduced_75_payout_factor': 0.60})
        line.invalidate_recordset([
            'margin_payout_factor',
            'margin_adjusted_gp_amount',
            'commission_basis_amount',
            'calculated_commission_amount',
        ])
        summary.invalidate_recordset(['commission_basis_amount', 'calculated_commission_amount'])
        period.invalidate_recordset(['margin_adjusted_commissionable_gp_amount', 'team_commission_total'])

        self.assertEqual(line.margin_protection_band, 'reduced_75')
        self.assertAlmostEqual(line.policy_margin_payout_factor, 0.75, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.60, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 15.0, places=2)
        self.assertAlmostEqual(line.commission_basis_amount, 15.0, places=2)
        self.assertAlmostEqual(line.calculated_commission_amount, 1.5, places=2)
        self.assertAlmostEqual(summary.commission_basis_amount, 15.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 15.0, places=2)
        self.assertAlmostEqual(period.team_commission_total, 1.5, places=2)

    def test_summary_full_band_gp_eligible_drives_full_payout_line(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.30)
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        summary.write({'margin_full_payout_factor': 0.90})
        line.invalidate_recordset(['margin_payout_factor', 'margin_adjusted_gp_amount'])
        period.invalidate_recordset(['margin_adjusted_commissionable_gp_amount'])

        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.policy_margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.90, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 27.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 27.0, places=2)

    def test_summary_gp_eligible_overrides_survive_recalculation(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.25)
        period = self._create_period()
        period.action_calculate()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        summary.write({'margin_reduced_75_payout_factor': 0.60})
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        self.assertAlmostEqual(summary.margin_reduced_75_payout_factor, 0.60, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.60, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 15.0, places=2)

    def test_summary_gp_eligible_rejects_above_100_percent(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            summary.write({'margin_reduced_75_payout_factor': 1.5})

    def test_summary_gp_eligible_rejects_zero_percent(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.action_calculate()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            summary.write({'margin_full_payout_factor': 0.0})

    def test_direct_salesperson_write_requires_reassign_override(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        other_user = self._create_commission_user('direct_reassign_blocked', 'Direct Reassign Blocked')
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids[:1]

        with self.assertRaises(ValidationError):
            line.write({'salesperson_id': other_user.id})

    def test_manager_reassign_override_moves_line_to_new_salesperson_summary(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        reassigned_user = self._create_commission_user('reassigned_owner', 'Reassigned Owner')
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.05
        period.action_calculate()
        line = period.line_ids[:1]

        line.write({
            'manual_commission_state': 'reassign',
            'manual_exclusion_reason': 'reassigned',
            'manual_salesperson_id': reassigned_user.id,
            'manager_note': 'Split handled by manager; pay reassigned owner.',
        })

        self.assertEqual(line.account_salesperson_id, self.env.user)
        self.assertEqual(line.salesperson_id, reassigned_user)
        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertTrue(line.override_user_id)
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == reassigned_user)
        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary.final_approved_commission_amount, 2.0, places=2)

    def test_split_line_excluded_from_totals_and_recovered_by_gp_adjustment(self):
        self._create_posted_invoice_from_sale(quantity=1.0, price_unit=100.0, purchase_price=60.0)
        period = self._create_period()
        period.plan_id.default_commission_rate = 0.05
        period.action_calculate()
        line = period.line_ids[:1]

        line.write({
            'manual_commission_state': 'split',
            'manual_exclusion_reason': 'split_commission',
            'manager_note': 'Per CFO 5/22 - split handled by manual adjustment.',
        })

        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'split_commission')
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 0.0, places=2)

        self.env['unico.commission.gp.adjustment'].create({
            'period_id': period.id,
            'salesperson_id': self.env.user.id,
            'amount': 40.0,
            'reason': 'split_commission',
            'note': 'Manual GP basis recovery for split commission.',
        })
        period._sync_salesperson_summaries()
        summary = period.summary_ids.filtered(lambda row: row.salesperson_id == self.env.user)

        self.assertAlmostEqual(period.adjusted_commissionable_gp_amount, 40.0, places=2)
        self.assertAlmostEqual(summary.gp_basis_adjustment_amount, 40.0, places=2)
        self.assertAlmostEqual(summary.commission_basis_amount, 40.0, places=2)
        self.assertAlmostEqual(summary.final_approved_commission_amount, 2.0, places=2)

    def test_gp_basis_adjustment_requires_salesperson(self):
        period = self._create_period()

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['unico.commission.gp.adjustment'].create({
                'period_id': period.id,
                'amount': 40.0,
                'reason': 'other',
                'note': 'Cannot allocate without salesperson.',
            })

    def test_gp_basis_adjustment_defaults_period_salesperson(self):
        period = self._create_period(salesperson=self.env.user)

        adjustment = self.env['unico.commission.gp.adjustment'].create({
            'period_id': period.id,
            'amount': 40.0,
            'reason': 'other',
            'note': 'Defaults period salesperson.',
        })

        self.assertEqual(adjustment.salesperson_id, self.env.user)

    def test_credit_note_reverses_gp_in_posted_period(self):
        __order, sale_line, __invoice = self._create_posted_invoice_from_sale(quantity=1.0)
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'invoice_user_id': self.env.user.id,
            'invoice_date': date(2026, 5, 20),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'sale_line_ids': [(6, 0, sale_line.ids)],
            })],
        })
        refund.action_post()
        self._pay_invoice(refund, payment_date=date(2026, 5, 20))

        period = self._create_period()
        period.action_calculate()

        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == refund)
        self.assertEqual(len(line), 1)
        self.assertTrue(line.is_credit)
        self.assertTrue(line.commissionable)
        self.assertAlmostEqual(line.revenue_amount, -100.0, places=2)
        self.assertAlmostEqual(line.cost_amount, -60.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, -40.0, places=2)
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, -40.0, places=2)

    def test_product_exclusion_can_be_manager_overridden(self):
        self._create_posted_invoice_from_sale(product=self.excluded_product, quantity=1.0, price_unit=50.0)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.product_id == self.excluded_product)

        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'product_excluded')

        line.write({
            'manual_commission_state': 'include',
            'manual_exclusion_reason': 'manager_override',
            'manager_note': 'Approved product exception',
        })

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 30.0, places=2)

    def test_manual_include_of_orphan_line_requires_reassign(self):
        self.partner.user_id = False
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale()
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        self.assertFalse(line.salesperson_id)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            line.write({
                'manual_commission_state': 'include',
                'manual_exclusion_reason': 'manager_override',
                'manager_note': 'Approved but ownership is still missing.',
            })

        line.write({
            'manual_commission_state': 'reassign',
            'manual_exclusion_reason': 'reassigned',
            'manual_salesperson_id': self.env.user.id,
            'manager_note': 'Assign commission ownership before including.',
        })

        self.assertTrue(line.commissionable)
        self.assertEqual(line.salesperson_id, self.env.user)

    def test_manual_invoice_without_sale_line_is_exception_not_guess(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_user_id': self.env.user.id,
            'invoice_date': date(2026, 5, 12),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        invoice.action_post()

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(line), 1)
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'missing_sale_line')
        self.assertEqual(line.salesperson_id, self.env.user)
        self.assertEqual(line.attribution_source, 'invoice_snapshot')
        self.assertAlmostEqual(line.cost_amount, 0.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 0.0, places=2)
        self.assertEqual(line.margin_protection_band, 'not_applicable')
        self.assertAlmostEqual(line.margin_payout_factor, 0.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 0.0, places=2)

    def test_recalculation_preserves_manager_overrides(self):
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale()
        __excluded_order, __excluded_sale_line, excluded_invoice = self._create_posted_invoice_from_sale(
            product=self.excluded_product,
            quantity=1.0,
            price_unit=50.0,
        )
        period = self._create_period()
        period.action_calculate()

        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        excluded_line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == excluded_invoice)
        line.write({
            'manual_commission_state': 'exclude',
            'manual_exclusion_reason': 'special_case',
            'manager_note': 'Per CFO 5/15',
        })
        excluded_line.write({
            'manual_commission_state': 'include',
            'manual_exclusion_reason': 'manager_override',
            'manager_note': 'Approved product exception',
        })

        period.action_calculate()

        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)
        excluded_line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == excluded_invoice)
        self.assertEqual(line.manual_commission_state, 'exclude')
        self.assertEqual(line.manual_exclusion_reason, 'special_case')
        self.assertEqual(line.manager_note, 'Per CFO 5/15')
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'special_case')
        self.assertEqual(excluded_line.manual_commission_state, 'include')
        self.assertEqual(excluded_line.manager_note, 'Approved product exception')
        self.assertTrue(excluded_line.commissionable)

    def test_zero_cost_sale_line_is_missing_cost_exception(self):
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(purchase_price=0.0)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(line), 1)
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'missing_cost')
        self.assertAlmostEqual(line.revenue_amount, 200.0, places=2)
        self.assertAlmostEqual(line.cost_amount, 0.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 0.0, places=2)
        self.assertAlmostEqual(line.margin_percent, 0.0, places=2)
        self.assertEqual(line.margin_protection_band, 'not_applicable')
        self.assertAlmostEqual(line.margin_payout_factor, 0.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 0.0, places=2)

    def test_cpq_manufacturing_cost_replaces_missing_shell_product_cost(self):
        if 'cpq_total_manufacturing_cost' not in self.env['sale.order.line']._fields:
            self.skipTest('klouded_cpq is not installed')

        __order, sale_line, invoice = self._create_posted_invoice_from_sale(
            quantity=1.0,
            price_unit=150.0,
            purchase_price=0.0,
        )
        if 'is_klouded_configurable' in sale_line.product_template_id._fields:
            sale_line.product_template_id.is_klouded_configurable = True

        component = self.env['product.product'].create({
            'name': 'CPQ Commission Component',
            'type': 'consu',
            'standard_price': 45.0,
        })
        workcenter = self.env['mrp.workcenter'].create({
            'name': 'CPQ Commission Workcenter',
            'costs_hour': 60.0,
        })
        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': sale_line.product_template_id.id,
            'product_id': sale_line.product_id.id,
            'type': 'normal',
            'bom_line_ids': [(0, 0, {
                'product_id': component.id,
                'product_qty': 1.0,
            })],
            'operation_ids': [(0, 0, {
                'name': 'CPQ Commission Operation',
                'workcenter_id': workcenter.id,
                'time_cycle': 15.0,
            })],
        })
        cpq_values = {'cpq_bom_id': bom.id}
        if 'cpq_configuration_data' in sale_line._fields:
            cpq_values['cpq_configuration_data'] = '{"door_style": "shaker"}'
        sale_line.write(cpq_values)
        sale_line.invalidate_recordset()
        expected_cost = sale_line.cpq_total_manufacturing_cost
        self.assertGreater(expected_cost, 0.0)

        sale_line.purchase_price = 0.0
        sale_line.invalidate_recordset(['purchase_price'])

        period = self._create_period()
        self.assertAlmostEqual(period._get_sale_line_unit_cost(sale_line), expected_cost, places=2)

        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(len(line), 1)
        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertAlmostEqual(line.revenue_amount, 150.0, places=2)
        self.assertAlmostEqual(line.cost_amount, expected_cost, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 150.0 - expected_cost, places=2)
        self.assertAlmostEqual(
            line.margin_percent,
            (150.0 - expected_cost) / 150.0,
            places=2,
        )

    def test_margin_protection_full_payout_starts_at_28_percent(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.28)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.margin_percent, 0.28, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 28.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 28.0, places=2)
        self.assertEqual(period.full_payout_line_count, 1)
        self.assertAlmostEqual(period.full_margin_gross_profit_amount, 28.0, places=2)

    def test_margin_protection_float_tolerance_keeps_exact_28_percent_in_full_band(self):
        period = self._create_period()

        band, factor = period._get_margin_protection_values(0.27999999999999997, True)

        self.assertEqual(band, 'full_payout')
        self.assertAlmostEqual(factor, 1.0, places=2)

    def test_margin_protection_25_percent_boundary_is_75_percent_band(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.25)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'reduced_75')
        self.assertAlmostEqual(line.margin_percent, 0.25, places=2)
        self.assertAlmostEqual(line.policy_margin_payout_factor, 0.75, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.75, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 18.75, places=2)

    def test_margin_protection_reduces_25_to_2799_percent_to_75_percent(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.27)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'reduced_75')
        self.assertAlmostEqual(line.margin_percent, 0.27, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.75, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 27.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 20.25, places=2)
        self.assertEqual(period.reduced_75_payout_line_count, 1)
        self.assertAlmostEqual(period.reduced_margin_gross_profit_amount, 27.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 20.25, places=2)

    def test_margin_protection_20_percent_boundary_is_50_percent_band(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.20)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'reduced_50')
        self.assertAlmostEqual(line.margin_percent, 0.20, places=2)
        self.assertAlmostEqual(line.policy_margin_payout_factor, 0.50, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.50, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 10.0, places=2)

    def test_margin_protection_reduces_20_to_2499_percent_to_50_percent(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.24)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertTrue(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'none')
        self.assertEqual(line.margin_protection_band, 'reduced_50')
        self.assertAlmostEqual(line.margin_percent, 0.24, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.50, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 24.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 12.0, places=2)
        self.assertEqual(period.reduced_50_payout_line_count, 1)
        self.assertAlmostEqual(period.reduced_margin_gross_profit_amount, 24.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 12.0, places=2)

    def test_margin_protection_excludes_below_20_percent_as_no_payout(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.19)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertFalse(line.commissionable)
        self.assertEqual(line.auto_exclusion_reason, 'low_margin_no_payout')
        self.assertEqual(line.effective_exclusion_reason, 'low_margin_no_payout')
        self.assertEqual(line.margin_protection_band, 'no_payout')
        self.assertAlmostEqual(line.margin_percent, 0.19, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 0.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 19.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 0.0, places=2)
        self.assertEqual(period.no_payout_line_count, 1)
        self.assertAlmostEqual(period.no_payout_gross_profit_amount, 19.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 0.0, places=2)

    def test_manual_include_on_no_payout_line_restores_adjusted_gp(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.19)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        line.write({
            'manual_commission_state': 'include',
            'manual_exclusion_reason': 'manager_override',
            'manager_note': 'Approved low-margin exception',
        })

        self.assertTrue(line.commissionable)
        self.assertEqual(line.margin_protection_band, 'no_payout')
        self.assertAlmostEqual(line.policy_margin_payout_factor, 0.0, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 19.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 19.0, places=2)

    def test_manual_exclude_on_full_payout_line_keeps_reference_adjusted_gp_out_of_totals(self):
        __order, __sale_line, invoice = self._create_posted_invoice_at_margin(0.30)

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        line.write({
            'manual_commission_state': 'exclude',
            'manual_exclusion_reason': 'manager_excluded',
            'manager_note': 'Manager exclusion test',
        })

        self.assertFalse(line.commissionable)
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.policy_margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_payout_factor, 1.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 30.0, places=2)
        self.assertAlmostEqual(period.margin_adjusted_commissionable_gp_amount, 0.0, places=2)

    def test_credit_note_uses_matching_reduced_margin_factor(self):
        __order, sale_line, __invoice = self._create_posted_invoice_at_margin(0.24)
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'invoice_user_id': self.env.user.id,
            'invoice_date': date(2026, 5, 20),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'sale_line_ids': [(6, 0, sale_line.ids)],
            })],
        })
        refund.action_post()
        self._pay_invoice(refund, payment_date=date(2026, 5, 20))

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == refund)

        self.assertTrue(line.is_credit)
        self.assertTrue(line.commissionable)
        self.assertEqual(line.margin_protection_band, 'reduced_50')
        self.assertAlmostEqual(line.margin_payout_factor, 0.50, places=2)
        self.assertAlmostEqual(line.revenue_amount, -100.0, places=2)
        self.assertAlmostEqual(line.cost_amount, -76.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, -24.0, places=2)
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, -12.0, places=2)

    def test_non_manager_cannot_calculate_period(self):
        user = self._create_commission_user('commission_user_no_calc', 'Commission User No Calc')
        period = self._create_period(salesperson=user)

        with self.assertRaises(AccessError):
            period.with_user(user).action_calculate()

    def test_salesperson_cannot_read_all_salespeople_period_summary(self):
        user = self._create_commission_user('commission_user_period_rule', 'Commission User Period Rule')
        period = self._create_period()

        period_model = self.env['unico.commission.gp.period'].with_user(user)
        self.assertFalse(period_model.search([('id', '=', period.id)]))
        with self.assertRaises(AccessError):
            period.with_user(user).check_access('read')

    def test_salesperson_sees_commission_only_after_review_submission(self):
        user = self._create_commission_user('commission_user_published_only', 'Published Only User')
        self.partner.user_id = user.id
        self._create_posted_invoice_from_sale()
        period = self._create_period(salesperson=user)
        period.action_calculate()

        line = period.line_ids.filtered(lambda gp_line: gp_line.salesperson_id == user)
        summary = period.summary_ids.filtered(lambda gp_summary: gp_summary.salesperson_id == user)
        self.assertTrue(line)
        self.assertTrue(summary)

        line_model = self.env['unico.commission.gp.line'].with_user(user)
        summary_model = self.env['unico.commission.gp.salesperson.summary'].with_user(user)
        period_model = self.env['unico.commission.gp.period'].with_user(user)

        def assert_visible_to_salesperson(visible):
            expected_line = line.id if visible else False
            expected_summary = summary.id if visible else False
            expected_period = period.id if visible else False
            self.assertEqual(line_model.search([('id', '=', line.id)]).id, expected_line)
            self.assertEqual(summary_model.search([('id', '=', summary.id)]).id, expected_summary)
            self.assertEqual(period_model.search([('id', '=', period.id)]).id, expected_period)

        self.assertEqual(period.state, 'draft')
        assert_visible_to_salesperson(False)
        draft_detail_action = summary.action_view_detail_lines()
        self.assertEqual(self.env['unico.commission.gp.line'].search(draft_detail_action['domain']), line)

        period.action_submit_for_review()
        self.assertEqual(period.state, PUBLISHED_STATES[0])
        assert_visible_to_salesperson(True)

        period.action_approve()
        self.assertEqual(period.state, PUBLISHED_STATES[1])
        assert_visible_to_salesperson(True)

        period.action_export_payroll()
        self.assertEqual(period.state, PUBLISHED_STATES[2])
        assert_visible_to_salesperson(True)

    def test_foreign_currency_invoice_amounts_convert_to_company_currency(self):
        foreign_currency = self.env.ref('base.EUR')
        if foreign_currency == self.env.company.currency_id:
            foreign_currency = self.env.ref('base.USD')
        foreign_currency.active = True
        rate = self.env['res.currency.rate'].search([
            ('currency_id', '=', foreign_currency.id),
            ('company_id', '=', self.env.company.id),
            ('name', '=', date(2026, 5, 15)),
        ], limit=1)
        rate_values = {
            'currency_id': foreign_currency.id,
            'company_id': self.env.company.id,
            'name': date(2026, 5, 15),
            'rate': 2.0,
        }
        if rate:
            rate.write(rate_values)
        else:
            self.env['res.currency.rate'].create(rate_values)
        pricelist = self.env['product.pricelist'].create({
            'name': 'Commission Foreign Currency',
            'currency_id': foreign_currency.id,
        })
        __order, __sale_line, invoice = self._create_posted_invoice_from_sale(
            quantity=1.0,
            price_unit=100.0,
            purchase_price=50.0,
            pricelist=pricelist,
        )

        period = self._create_period()
        period.action_calculate()
        line = period.line_ids.filtered(lambda gp_line: gp_line.move_id == invoice)

        self.assertEqual(line.currency_id, self.env.company.currency_id)
        self.assertAlmostEqual(line.revenue_amount, 50.0, places=2)
        self.assertAlmostEqual(line.cost_amount, 25.0, places=2)
        self.assertAlmostEqual(line.gross_profit_amount, 25.0, places=2)
        self.assertEqual(line.margin_protection_band, 'full_payout')
        self.assertAlmostEqual(line.margin_adjusted_gp_amount, 25.0, places=2)

    def test_manual_exclusion_requires_reason(self):
        self._create_posted_invoice_from_sale()
        period = self._create_period()
        period.action_calculate()
        line = period.line_ids[:1]

        with self.assertRaises(ValidationError):
            line.write({'manual_commission_state': 'exclude'})

        line.write({
            'manual_commission_state': 'exclude',
            'manual_exclusion_reason': 'manager_excluded',
            'manager_note': 'Reason supplied',
        })
        self.assertFalse(line.commissionable)
        self.assertEqual(line.effective_exclusion_reason, 'manager_excluded')

    def test_closed_period_blocks_line_and_adjustment_changes(self):
        self._create_posted_invoice_from_sale()
        period = self._create_period()
        period.action_calculate()
        period.action_submit_for_review()
        period.action_approve()

        with self.assertRaises(UserError):
            period.line_ids[:1].write({'manual_commission_state': 'exclude'})

        with self.assertRaises(UserError):
            self.env['unico.commission.gp.adjustment'].create({
                'period_id': period.id,
                'amount': 10.0,
                'reason': 'other',
                'note': 'Late adjustment',
            })
