# -*- coding: utf-8 -*-

from odoo import api, fields, models


COMMISSION_ATTRIBUTION_SOURCES = [
    ('account_owner', 'Account Salesperson'),
    ('sale_order_snapshot', 'Sales Order Snapshot'),
    ('invoice_snapshot', 'Invoice Snapshot'),
    ('current_account_owner', 'Current Account Salesperson'),
    ('missing_account_salesperson', 'Missing Account Salesperson'),
    ('multiple_sales_orders', 'Multiple Sales Orders'),
]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    unico_commission_account_salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Account Salesperson Snapshot',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_partner_id = fields.Many2one(
        'res.partner',
        string='Commission Attribution Account',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_source = fields.Selection(
        COMMISSION_ATTRIBUTION_SOURCES,
        string='Commission Attribution Source',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_snapshot_date = fields.Datetime(
        string='Commission Attribution Snapshot Date',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_snapshot_user_id = fields.Many2one(
        'res.users',
        string='Commission Attribution Snapshotted By',
        copy=False,
        readonly=True,
    )

    def _unico_get_commission_account_partner(self):
        self.ensure_one()
        return self.partner_id.commercial_partner_id

    def _unico_get_commission_account_salesperson(self):
        self.ensure_one()
        account_partner = self._unico_get_commission_account_partner()
        return account_partner.user_id if account_partner else self.env['res.users']

    def _unico_current_user_has_personal_sales_scope(self):
        return (
            self.env.user.has_group('sales_team.group_sale_salesman')
            and not self.env.user.has_group('sales_team.group_sale_salesman_all_leads')
        )

    def _unico_prepare_salesperson_owner_vals(self, partner_id):
        if not partner_id or self.env.context.get('skip_unico_commission_salesperson_sync'):
            return {}
        if self._unico_current_user_has_personal_sales_scope():
            return {'user_id': self.env.user.id}

        account_partner = self.env['res.partner'].browse(partner_id).commercial_partner_id
        if account_partner.user_id:
            return {'user_id': account_partner.user_id.id}
        return {}

    def _unico_prepare_commission_snapshot_vals(self, source='account_owner'):
        self.ensure_one()
        account_partner = self._unico_get_commission_account_partner()
        salesperson = account_partner.user_id if account_partner else self.env['res.users']
        return {
            'unico_commission_account_salesperson_id': salesperson.id if salesperson else False,
            'unico_commission_attribution_partner_id': account_partner.id if account_partner else False,
            'unico_commission_attribution_source': source if salesperson else 'missing_account_salesperson',
            'unico_commission_attribution_snapshot_date': fields.Datetime.now(),
            'unico_commission_attribution_snapshot_user_id': self.env.user.id,
        }

    def _unico_sync_quote_salesperson_owner(self):
        for order in self:
            if order._unico_current_user_has_personal_sales_scope():
                if order.partner_id and order.user_id != self.env.user:
                    order.user_id = self.env.user
                continue
            salesperson = order._unico_get_commission_account_salesperson()
            if salesperson and order.user_id != salesperson:
                order.user_id = salesperson

    @api.depends('partner_id')
    def _compute_user_id(self):
        super()._compute_user_id()
        for order in self:
            if not order.partner_id:
                continue
            # Odoo's own-doc sale rules key off sale.order.user_id / sale.order.line.salesman_id.
            # Under that scope, the active quote owner must remain the acting user.
            if order._unico_current_user_has_personal_sales_scope():
                order.user_id = self.env.user
                continue
            salesperson = order._unico_get_commission_account_salesperson()
            if salesperson:
                order.user_id = salesperson

    @api.onchange('partner_id')
    def _onchange_unico_commission_partner_id(self):
        self._unico_sync_quote_salesperson_owner()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if (
                vals.get('partner_id')
                and 'user_id' not in vals
                and not self.env.context.get('skip_unico_commission_salesperson_sync')
            ):
                vals.update(self._unico_prepare_salesperson_owner_vals(vals['partner_id']))
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if (
            vals.get('partner_id')
            and not self.env.context.get('skip_unico_commission_salesperson_sync')
            and 'user_id' not in vals
        ):
            vals.update(self._unico_prepare_salesperson_owner_vals(vals['partner_id']))
        return super().write(vals)

    def action_confirm(self):
        for order in self:
            if not order.unico_commission_attribution_snapshot_date:
                order.write(order._unico_prepare_commission_snapshot_vals('account_owner'))
        return super().action_confirm()

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        self.ensure_one()
        if not self.unico_commission_attribution_snapshot_date:
            # Invoices can be prepared from imported or otherwise unsnapshotted orders; capture once before
            # propagating attribution values so invoice-based commission reports remain auditable.
            self.write(self._unico_prepare_commission_snapshot_vals('account_owner'))
        vals.update({
            'unico_commission_account_salesperson_id': self.unico_commission_account_salesperson_id.id,
            'unico_commission_attribution_partner_id': self.unico_commission_attribution_partner_id.id,
            'unico_commission_attribution_source': (
                'sale_order_snapshot'
                if self.unico_commission_account_salesperson_id
                else 'missing_account_salesperson'
            ),
            'unico_commission_attribution_snapshot_date': (
                self.unico_commission_attribution_snapshot_date or fields.Datetime.now()
            ),
            'unico_commission_attribution_snapshot_user_id': (
                self.unico_commission_attribution_snapshot_user_id.id or self.env.user.id
            ),
        })
        return vals


class AccountMove(models.Model):
    _inherit = 'account.move'

    unico_commission_account_salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Account Salesperson Snapshot',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_partner_id = fields.Many2one(
        'res.partner',
        string='Commission Attribution Account',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_source = fields.Selection(
        COMMISSION_ATTRIBUTION_SOURCES,
        string='Commission Attribution Source',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_snapshot_date = fields.Datetime(
        string='Commission Attribution Snapshot Date',
        copy=False,
        readonly=True,
    )
    unico_commission_attribution_snapshot_user_id = fields.Many2one(
        'res.users',
        string='Commission Attribution Snapshotted By',
        copy=False,
        readonly=True,
    )
    unico_commission_cash_collected = fields.Boolean(
        string='Commission Cash Collected',
        compute='_compute_unico_commission_collection',
        store=True,
        index=True,
        copy=False,
        help=(
            "True when the customer invoice or refund is paid through an actual payment, bank, "
            "or cash journal for commission eligibility. Credit-note-only settlement is not cash collected."
        ),
    )
    unico_commission_collection_date = fields.Date(
        string='Commission Collection Date',
        compute='_compute_unico_commission_collection',
        store=True,
        index=True,
        copy=False,
        help="Latest cash, bank, or payment date used to place paid invoices into commission periods.",
    )

    def _unico_get_commission_sale_order_snapshots(self):
        self.ensure_one()
        return self.invoice_line_ids.sale_line_ids.order_id.filtered(
            'unico_commission_attribution_snapshot_date'
        )

    def _unico_prepare_commission_snapshot_vals(self):
        self.ensure_one()
        sale_orders = self.invoice_line_ids.sale_line_ids.order_id
        snapshotted_orders = sale_orders.filtered('unico_commission_attribution_snapshot_date')
        if len(snapshotted_orders) == 1:
            order = snapshotted_orders
            salesperson = order.unico_commission_account_salesperson_id
            return {
                'unico_commission_account_salesperson_id': salesperson.id if salesperson else False,
                'unico_commission_attribution_partner_id': order.unico_commission_attribution_partner_id.id,
                'unico_commission_attribution_source': (
                    'sale_order_snapshot' if salesperson else 'missing_account_salesperson'
                ),
                'unico_commission_attribution_snapshot_date': (
                    order.unico_commission_attribution_snapshot_date or fields.Datetime.now()
                ),
                'unico_commission_attribution_snapshot_user_id': (
                    order.unico_commission_attribution_snapshot_user_id.id or self.env.user.id
                ),
            }
        if len(sale_orders) > 1:
            return {
                'unico_commission_account_salesperson_id': False,
                'unico_commission_attribution_partner_id': self.commercial_partner_id.id,
                'unico_commission_attribution_source': 'multiple_sales_orders',
                'unico_commission_attribution_snapshot_date': fields.Datetime.now(),
                'unico_commission_attribution_snapshot_user_id': self.env.user.id,
            }

        account_partner = self.commercial_partner_id
        salesperson = account_partner.user_id if account_partner else self.env['res.users']
        return {
            'unico_commission_account_salesperson_id': salesperson.id if salesperson else False,
            'unico_commission_attribution_partner_id': account_partner.id if account_partner else False,
            'unico_commission_attribution_source': (
                'invoice_snapshot' if salesperson else 'missing_account_salesperson'
            ),
            'unico_commission_attribution_snapshot_date': fields.Datetime.now(),
            'unico_commission_attribution_snapshot_user_id': self.env.user.id,
        }

    def _unico_commission_get_cash_collection_date(self):
        self.ensure_one()
        payment_dates = [
            payment.date
            for payment in self.reconciled_payment_ids
            if payment.state == 'paid' and payment.date
        ]
        statement_dates = [
            statement_line.date
            for statement_line in self._get_reconciled_statement_lines()
            if statement_line.date
        ]
        collection_dates = payment_dates + statement_dates
        if collection_dates:
            return max(collection_dates)
        return False

    def _unico_commission_has_cash_collection(self):
        self.ensure_one()
        return bool(
            self.reconciled_payment_ids.filtered(lambda payment: payment.state == 'paid')
            or self._get_reconciled_statement_lines()
        )

    @api.depends(
        'state',
        'move_type',
        'payment_state',
        'reconciled_payment_ids.state',
        'reconciled_payment_ids.date',
        'date',
        'invoice_date',
        'line_ids.matched_debit_ids.max_date',
        'line_ids.matched_credit_ids.max_date',
    )
    def _compute_unico_commission_collection(self):
        for move in self:
            if move.state != 'posted' or move.move_type not in ('out_invoice', 'out_refund'):
                move.unico_commission_cash_collected = False
                move.unico_commission_collection_date = False
                continue

            # Commission eligibility is strict cash collection, not just paid/settled status.
            # Credit-note-only reconciliations can make an invoice paid, but have no payment or
            # bank statement behind them and must remain pending for commission purposes.
            collection_date = move._unico_commission_get_cash_collection_date()
            collected = (
                move.payment_state == 'paid'
                and bool(collection_date)
                and move._unico_commission_has_cash_collection()
            )
            move.unico_commission_cash_collected = collected
            move.unico_commission_collection_date = collection_date if collected else False

    def action_post(self):
        res = super().action_post()
        invoices = self.filtered(lambda move: move.state == 'posted' and move.move_type in ('out_invoice', 'out_refund'))
        for invoice in invoices:
            if not invoice.unico_commission_attribution_snapshot_date:
                invoice.write(invoice._unico_prepare_commission_snapshot_vals())
        return res
