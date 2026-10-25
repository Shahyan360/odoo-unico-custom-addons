# -*- coding: utf-8 -*-

import base64
import csv
import io

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import date_utils, float_compare, float_is_zero, format_date


INVOICE_MOVE_TYPES = ('out_invoice', 'out_refund')
PUBLISHED_STATES = ['ready_for_review', 'approved', 'exported']
INVOICE_PAYMENT_STATES = [
    ('not_paid', 'Not Paid'),
    ('in_payment', 'In Payment'),
    ('paid', 'Paid'),
    ('partial', 'Partially Paid'),
    ('reversed', 'Reversed'),
    ('blocked', 'Blocked'),
    ('invoicing_legacy', 'Invoicing App Legacy'),
]
DEFAULT_FULL_PAYOUT_MARGIN_THRESHOLD = 0.28
DEFAULT_REDUCED_75_MARGIN_THRESHOLD = 0.25
DEFAULT_REDUCED_50_MARGIN_THRESHOLD = 0.20
DEFAULT_FULL_PAYOUT_FACTOR = 1.00
DEFAULT_REDUCED_75_PAYOUT_FACTOR = 0.75
DEFAULT_REDUCED_50_PAYOUT_FACTOR = 0.50
MARGIN_BAND_FACTORS = {
    'full_payout': (
        'margin_full_payout_factor',
        DEFAULT_FULL_PAYOUT_FACTOR,
        'Full Band GP Eligible',
    ),
    'reduced_75': (
        'margin_reduced_75_payout_factor',
        DEFAULT_REDUCED_75_PAYOUT_FACTOR,
        'Reduced Band GP Eligible',
    ),
    'reduced_50': (
        'margin_reduced_50_payout_factor',
        DEFAULT_REDUCED_50_PAYOUT_FACTOR,
        'Low Margin Band GP Eligible',
    ),
}
MARGIN_BAND_FACTOR_FIELDS = tuple(
    field for field, __default, __label in MARGIN_BAND_FACTORS.values()
)


class UnicoCommissionGpPeriod(models.Model):
    _name = 'unico.commission.gp.period'
    _description = 'Commission Period'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_start desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    date_start = fields.Date(required=True, tracking=True)
    date_end = fields.Date(required=True, tracking=True)
    salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Salesperson',
        tracking=True,
        help="Optional. Leave blank to calculate all salespeople for the month.",
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        tracking=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='company_id.currency_id',
        store=True,
        readonly=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('ready_for_review', 'Ready for Review'),
            ('approved', 'Approved'),
            ('exported', 'Exported to Payroll'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        tracking=True,
    )
    last_calculated_date = fields.Datetime(string='Last Calculated', readonly=True)
    exported_date = fields.Datetime(string='Exported Date', readonly=True)
    exported_by_id = fields.Many2one('res.users', string='Exported By', readonly=True)
    line_ids = fields.One2many(
        'unico.commission.gp.line',
        'period_id',
        string='Transaction Lines',
    )
    adjustment_ids = fields.One2many(
        'unico.commission.gp.adjustment',
        'period_id',
        string='GP Basis Adjustments',
    )
    summary_ids = fields.One2many(
        'unico.commission.gp.salesperson.summary',
        'period_id',
        string='Commission by Salesperson',
    )
    plan_id = fields.Many2one(
        'commission.plan',
        string='Commission Plan',
        required=True,
        default=lambda self: self.env['commission.plan'].search(
            [('company_id', 'in', [self.env.company.id, False])], limit=1,
        ),
        tracking=True,
    )
    require_cash_collected = fields.Boolean(
        string='Require Cash Collection', readonly=True, default=True,
        help="Snapshotted from the commission plan at Calculate time.",
    )
    attribution_basis = fields.Selection(
        [
            ('account_owner', 'Customer Account Owner'),
            ('sales_order_user', 'Sales Order Salesperson'),
            ('invoice_user', 'Invoice Salesperson'),
        ],
        string='Attribution Basis', readonly=True, default='account_owner',
        help="Snapshotted from the commission plan at Calculate time.",
    )
    default_commission_rate = fields.Float(
        string='Commission Rate',
        default=0.0,
        readonly=True,
        help="Snapshotted from the commission plan at Calculate time. Enter as a decimal: 0.10 means 10%.",
    )
    source_line_count = fields.Integer(compute='_compute_totals', store=True)
    exception_line_count = fields.Integer(compute='_compute_totals', store=True)
    total_revenue_amount = fields.Monetary(compute='_compute_totals', store=True)
    total_cost_amount = fields.Monetary(compute='_compute_totals', store=True)
    total_gross_profit_amount = fields.Monetary(compute='_compute_totals', store=True)
    commissionable_revenue_amount = fields.Monetary(compute='_compute_totals', store=True)
    commissionable_cost_amount = fields.Monetary(compute='_compute_totals', store=True)
    commissionable_gross_profit_amount = fields.Monetary(compute='_compute_totals', store=True)
    margin_adjusted_commissionable_gp_amount = fields.Monetary(
        string='GP After Margin Protection',
        compute='_compute_totals',
        store=True,
    )
    excluded_gross_profit_amount = fields.Monetary(compute='_compute_totals', store=True)
    full_margin_gross_profit_amount = fields.Monetary(
        string='Full Band Raw GP',
        compute='_compute_totals',
        store=True,
    )
    reduced_margin_gross_profit_amount = fields.Monetary(
        string='Reduced / Low-Margin Raw GP',
        compute='_compute_totals',
        store=True,
    )
    no_payout_gross_profit_amount = fields.Monetary(
        string='No-Payout Band Raw GP',
        compute='_compute_totals',
        store=True,
    )
    adjustment_amount = fields.Monetary(compute='_compute_totals', store=True)
    adjusted_commissionable_gp_amount = fields.Monetary(
        string='Final Commission Basis',
        compute='_compute_totals',
        store=True,
    )
    calculated_commission_amount = fields.Monetary(
        string='Calculated Commission Amount',
        compute='_compute_commission_totals',
        store=True,
    )
    manual_commission_adjustment_amount = fields.Monetary(
        string='Manual Commission Adjustment',
        compute='_compute_commission_totals',
        store=True,
    )
    final_approved_commission_amount = fields.Monetary(
        string='Final Approved Commission Amount',
        compute='_compute_commission_totals',
        store=True,
    )
    team_commission_total = fields.Monetary(
        string='Total Payable Commission',
        compute='_compute_commission_totals',
        store=True,
    )
    full_payout_line_count = fields.Integer(
        string='Full Band Line Count',
        compute='_compute_totals',
        store=True,
    )
    reduced_75_payout_line_count = fields.Integer(
        string='Reduced Band Line Count',
        compute='_compute_totals',
        store=True,
    )
    reduced_50_payout_line_count = fields.Integer(
        string='Low Margin Band Line Count',
        compute='_compute_totals',
        store=True,
    )
    no_payout_line_count = fields.Integer(
        string='No-Payout Band Line Count',
        compute='_compute_totals',
        store=True,
    )
    pending_collection_amount = fields.Monetary(compute='_compute_totals', store=True)
    pending_collection_line_count = fields.Integer(compute='_compute_totals', store=True)
    margin_full_payout_threshold = fields.Float(
        string='Full Band Starts At Margin',
        default=DEFAULT_FULL_PAYOUT_MARGIN_THRESHOLD,
        readonly=True,
        help="Snapshotted from the commission plan at Calculate time. 0.28 means 28%.",
    )
    margin_reduced_75_threshold = fields.Float(
        string='Reduced Band Starts At Margin',
        default=DEFAULT_REDUCED_75_MARGIN_THRESHOLD,
        readonly=True,
        help="Snapshotted from the commission plan at Calculate time.",
    )
    margin_reduced_50_threshold = fields.Float(
        string='Low Margin Band Starts At Margin',
        default=DEFAULT_REDUCED_50_MARGIN_THRESHOLD,
        readonly=True,
        help="Snapshotted from the commission plan at Calculate time.",
    )
    margin_full_payout_factor = fields.Float(
        string='Full Band GP Eligible',
        default=DEFAULT_FULL_PAYOUT_FACTOR,
        readonly=True,
        help=(
            "Default percent of GP eligible for commission in the full margin band. "
            "Snapshotted from the commission plan at Calculate time; salesperson rows can override it."
        ),
    )
    margin_reduced_75_payout_factor = fields.Float(
        string='Reduced Band GP Eligible',
        default=DEFAULT_REDUCED_75_PAYOUT_FACTOR,
        readonly=True,
        help=(
            "Default percent of GP eligible for commission in the reduced margin band. "
            "Snapshotted from the commission plan at Calculate time; salesperson rows can override it."
        ),
    )
    margin_reduced_50_payout_factor = fields.Float(
        string='Low Margin Band GP Eligible',
        default=DEFAULT_REDUCED_50_PAYOUT_FACTOR,
        readonly=True,
        help=(
            "Default percent of GP eligible for commission in the low-margin band. "
            "Snapshotted from the commission plan at Calculate time; salesperson rows can override it."
        ),
    )

    _date_range_check = models.Constraint(
        'CHECK(date_start <= date_end)',
        'The period start date must be before or equal to the end date.',
    )

    def init(self):
        # Upgrade bridge from the first release, where approved/locked periods
        # used state='closed'. Use the ORM so this stays aligned with normal
        # Odoo access, cache, and audit behavior.
        self.search([('state', '=', 'closed')]).write({'state': 'approved'})

    @api.constrains('default_commission_rate')
    def _check_default_commission_rate(self):
        for period in self:
            if not 0.0 <= (period.default_commission_rate or 0.0) <= 1.0:
                raise ValidationError(_(
                    "Commission rate must be between 0.00 and 1.00. Enter 0.10 for 10%."
                ))

    @api.constrains(*MARGIN_BAND_FACTOR_FIELDS)
    def _check_margin_payout_factors(self):
        for period in self:
            for field, __default, label in MARGIN_BAND_FACTORS.values():
                value = period[field] or 0.0
                if not 0.0 < value <= 1.0:
                    raise ValidationError(_(
                        "%(label)s must be greater than 0%% and no more than 100%%. Enter 0.75 for 75%%.",
                        label=_(label),
                    ))

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        today = fields.Date.context_today(self)
        month_start, month_end = date_utils.get_month(today)
        values.setdefault('date_start', month_start)
        values.setdefault('date_end', month_end)
        return values

    @api.depends('date_start', 'date_end', 'salesperson_id')
    def _compute_name(self):
        for period in self:
            if period.date_start and period.date_end:
                date_label = '%s - %s' % (
                    format_date(self.env, period.date_start),
                    format_date(self.env, period.date_end),
                )
            else:
                date_label = _('New Period')
            if period.salesperson_id:
                period.name = '%s - %s' % (date_label, period.salesperson_id.name)
            else:
                period.name = '%s - All Salespeople' % date_label

    @api.depends(
        'line_ids.revenue_amount',
        'line_ids.cost_amount',
        'line_ids.gross_profit_amount',
        'line_ids.margin_protection_band',
        'line_ids.margin_adjusted_gp_amount',
        'line_ids.commissionable',
        'line_ids.effective_exclusion_reason',
        'adjustment_ids.amount',
    )
    def _compute_totals(self):
        for period in self:
            lines = period.line_ids
            included_lines = lines.filtered('commissionable')
            excluded_lines = lines - included_lines
            full_payout_lines = lines.filtered(lambda line: line.margin_protection_band == 'full_payout')
            reduced_75_lines = lines.filtered(lambda line: line.margin_protection_band == 'reduced_75')
            reduced_50_lines = lines.filtered(lambda line: line.margin_protection_band == 'reduced_50')
            no_payout_lines = lines.filtered(lambda line: line.margin_protection_band == 'no_payout')
            pending_lines = lines.filtered(
                lambda line: line.effective_exclusion_reason == 'pending_collection'
            )
            adjustment_amount = sum(period.adjustment_ids.mapped('amount'))

            period.source_line_count = len(lines)
            period.exception_line_count = len(excluded_lines)
            period.pending_collection_line_count = len(pending_lines)
            period.pending_collection_amount = sum(pending_lines.mapped('gross_profit_amount'))
            period.total_revenue_amount = sum(lines.mapped('revenue_amount'))
            period.total_cost_amount = sum(lines.mapped('cost_amount'))
            period.total_gross_profit_amount = sum(lines.mapped('gross_profit_amount'))
            period.commissionable_revenue_amount = sum(included_lines.mapped('revenue_amount'))
            period.commissionable_cost_amount = sum(included_lines.mapped('cost_amount'))
            period.commissionable_gross_profit_amount = sum(included_lines.mapped('gross_profit_amount'))
            period.margin_adjusted_commissionable_gp_amount = sum(included_lines.mapped('margin_adjusted_gp_amount'))
            period.excluded_gross_profit_amount = sum(excluded_lines.mapped('gross_profit_amount'))
            period.full_margin_gross_profit_amount = sum(full_payout_lines.mapped('gross_profit_amount'))
            period.reduced_margin_gross_profit_amount = (
                sum(reduced_75_lines.mapped('gross_profit_amount'))
                + sum(reduced_50_lines.mapped('gross_profit_amount'))
            )
            period.no_payout_gross_profit_amount = sum(no_payout_lines.mapped('gross_profit_amount'))
            period.adjustment_amount = adjustment_amount
            period.adjusted_commissionable_gp_amount = (
                period.margin_adjusted_commissionable_gp_amount + adjustment_amount
            )
            period.full_payout_line_count = len(full_payout_lines)
            period.reduced_75_payout_line_count = len(reduced_75_lines)
            period.reduced_50_payout_line_count = len(reduced_50_lines)
            period.no_payout_line_count = len(no_payout_lines)

    @api.depends(
        'summary_ids.calculated_commission_amount',
        'summary_ids.manual_commission_adjustment_amount',
        'summary_ids.final_approved_commission_amount',
    )
    def _compute_commission_totals(self):
        for period in self:
            period.calculated_commission_amount = sum(period.summary_ids.mapped('calculated_commission_amount'))
            period.manual_commission_adjustment_amount = sum(
                period.summary_ids.mapped('manual_commission_adjustment_amount')
            )
            period.final_approved_commission_amount = sum(
                period.summary_ids.mapped('final_approved_commission_amount')
            )
            period.team_commission_total = period.final_approved_commission_amount

    def _check_manager_access(self):
        if not (
            self.env.user.has_group('unico_commission_gp_reporting.group_unico_commission_gp_manager')
            or self.env.user.has_group('unico_commission_gp_reporting.group_unico_commission_gp_admin')
        ):
            raise AccessError(_("Only commission GP managers can perform this action."))

    def _get_invoice_line_domain(self):
        self.ensure_one()
        return [
            ('company_id', '=', self.company_id.id),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', INVOICE_MOVE_TYPES),
            ('display_type', '=', 'product'),
            '|',
                '&',
                    ('move_id.invoice_date', '>=', self.date_start),
                    ('move_id.invoice_date', '<=', self.date_end),
                '&',
                    ('move_id.unico_commission_cash_collected', '=', True),
                    '&',
                        ('move_id.unico_commission_collection_date', '>=', self.date_start),
                        ('move_id.unico_commission_collection_date', '<=', self.date_end),
        ]

    def _get_line_direction(self, invoice_line):
        return -1.0 if invoice_line.move_id.move_type == 'out_refund' else 1.0

    def _get_line_salesperson(self, invoice_line):
        return self._get_line_attribution(invoice_line).get('salesperson')

    def _get_line_attribution(self, invoice_line):
        """Resolve commission ownership using the period's snapshotted attribution basis."""
        basis = self.attribution_basis or 'account_owner'
        if basis == 'sales_order_user':
            return self._attribution_from_sales_order_user(invoice_line)
        if basis == 'invoice_user':
            return self._attribution_from_invoice_user(invoice_line)
        return self._attribution_from_account_owner(invoice_line)

    def _attribution_from_sales_order_user(self, invoice_line):
        move = invoice_line.move_id
        sale_orders = invoice_line.sale_line_ids.filtered(
            lambda line: not line.display_type
        ).order_id
        empty_user = self.env['res.users']
        attribution = {
            'salesperson': empty_user, 'account_salesperson': empty_user,
            'partner': move.commercial_partner_id, 'source': 'missing_account_salesperson',
            'snapshot_date': False, 'snapshot_user': empty_user, 'warning': 'none',
        }
        if len(sale_orders) > 1:
            attribution['source'] = 'multiple_sales_orders'
            return attribution
        salesperson = sale_orders.user_id if len(sale_orders) == 1 else move.invoice_user_id
        if salesperson:
            attribution.update({
                'salesperson': salesperson, 'account_salesperson': salesperson,
                'partner': move.commercial_partner_id,
                'source': 'sales_order_user' if len(sale_orders) == 1 else 'invoice_user',
            })
        return attribution

    def _attribution_from_invoice_user(self, invoice_line):
        move = invoice_line.move_id
        empty_user = self.env['res.users']
        attribution = {
            'salesperson': empty_user, 'account_salesperson': empty_user,
            'partner': move.commercial_partner_id, 'source': 'missing_account_salesperson',
            'snapshot_date': False, 'snapshot_user': empty_user, 'warning': 'none',
        }
        if move.invoice_user_id:
            attribution.update({
                'salesperson': move.invoice_user_id,
                'account_salesperson': move.invoice_user_id,
                'partner': move.commercial_partner_id, 'source': 'invoice_user',
            })
        return attribution

    def _attribution_from_account_owner(self, invoice_line):
        """Resolve commission ownership from account-owner snapshots, not order entry."""
        move = invoice_line.move_id
        sale_lines = invoice_line.sale_line_ids.filtered(lambda line: not line.display_type)
        sale_orders = sale_lines.order_id
        attribution = {
            'salesperson': self.env['res.users'],
            'account_salesperson': self.env['res.users'],
            'partner': move.commercial_partner_id,
            'source': 'missing_account_salesperson',
            'snapshot_date': False,
            'snapshot_user': self.env['res.users'],
            'warning': 'none',
        }

        if len(sale_orders) > 1:
            attribution['source'] = 'multiple_sales_orders'
            return attribution

        if len(sale_orders) == 1:
            order = sale_orders
            snapshot_salesperson = order.unico_commission_account_salesperson_id
            snapshot_partner = order.unico_commission_attribution_partner_id or order.partner_id.commercial_partner_id
            if snapshot_salesperson:
                attribution.update({
                    'salesperson': snapshot_salesperson,
                    'account_salesperson': snapshot_salesperson,
                    'partner': snapshot_partner,
                    'source': 'sale_order_snapshot',
                    'snapshot_date': order.unico_commission_attribution_snapshot_date,
                    'snapshot_user': order.unico_commission_attribution_snapshot_user_id,
                })
                if snapshot_partner.user_id and snapshot_partner.user_id != snapshot_salesperson:
                    attribution['warning'] = 'account_salesperson_changed'
                return attribution
            if snapshot_partner.user_id:
                attribution.update({
                    'salesperson': snapshot_partner.user_id,
                    'account_salesperson': snapshot_partner.user_id,
                    'partner': snapshot_partner,
                    'source': 'current_account_owner',
                    'warning': 'missing_snapshot',
                })
                return attribution
            attribution['partner'] = snapshot_partner
            return attribution

        if move.unico_commission_account_salesperson_id:
            attribution.update({
                'salesperson': move.unico_commission_account_salesperson_id,
                'account_salesperson': move.unico_commission_account_salesperson_id,
                'partner': move.unico_commission_attribution_partner_id or move.commercial_partner_id,
                'source': move.unico_commission_attribution_source or 'invoice_snapshot',
                'snapshot_date': move.unico_commission_attribution_snapshot_date,
                'snapshot_user': move.unico_commission_attribution_snapshot_user_id,
            })
            account_partner = attribution['partner']
            if account_partner.user_id and account_partner.user_id != attribution['account_salesperson']:
                attribution['warning'] = 'account_salesperson_changed'
            return attribution

        account_partner = move.commercial_partner_id
        if account_partner.user_id:
            attribution.update({
                'salesperson': account_partner.user_id,
                'account_salesperson': account_partner.user_id,
                'partner': account_partner,
                'source': 'current_account_owner',
                'warning': 'missing_snapshot',
            })
        return attribution

    def _convert_amount_to_period_currency(self, amount, from_currency, conversion_date):
        self.ensure_one()
        from_currency = from_currency or self.currency_id
        if not from_currency or from_currency == self.currency_id:
            return amount
        return from_currency._convert(
            from_amount=amount,
            to_currency=self.currency_id,
            company=self.company_id,
            date=conversion_date or fields.Date.context_today(self),
            round=False,
        )

    def _get_sale_line_unit_cost(self, sale_line):
        """Return the authoritative unit cost for commission GP.

        Standard sale-margin cost remains the primary source. CPQ configured
        products can have a zero shell-product cost while their true quoted cost
        lives on the sale line as generated BOM material and operation costs.
        The CPQ fallback is intentionally defensive so this module does not
        require a hard dependency on klouded_cpq.
        """
        if not sale_line or len(sale_line) != 1:
            return 0.0

        unit_cost = sale_line.purchase_price or 0.0
        if unit_cost > 0.0:
            return unit_cost
        return self._get_sale_line_cpq_unit_cost(sale_line)

    def _get_sale_line_cpq_unit_cost(self, sale_line):
        if not sale_line or len(sale_line) != 1:
            return 0.0

        cpq_cost_getter = getattr(sale_line, '_get_cpq_purchase_price_unit', None)
        if callable(cpq_cost_getter):
            cpq_cost = cpq_cost_getter()
            if cpq_cost is not False and cpq_cost > 0.0:
                return cpq_cost

        fields_map = sale_line._fields
        cpq_cost_fields = {
            'cpq_total_manufacturing_cost',
            'cpq_material_cost',
            'cpq_operation_cost',
        }
        if not cpq_cost_fields.intersection(fields_map):
            return 0.0

        stored_total_cost = (
            sale_line['cpq_total_manufacturing_cost']
            if 'cpq_total_manufacturing_cost' in fields_map
            else 0.0
        ) or 0.0
        is_cpq_configured = (
            ('is_configurable' not in fields_map or sale_line.is_configurable)
            and (
                ('cpq_bom_id' in fields_map and sale_line.cpq_bom_id)
                or ('cpq_configuration_data' in fields_map and sale_line.cpq_configuration_data)
                or stored_total_cost > 0.0
            )
        )
        if not is_cpq_configured:
            return 0.0

        if stored_total_cost > 0.0:
            return stored_total_cost

        material_cost = (
            sale_line['cpq_material_cost']
            if 'cpq_material_cost' in fields_map
            else 0.0
        ) or 0.0
        operation_cost = (
            sale_line['cpq_operation_cost']
            if 'cpq_operation_cost' in fields_map
            else 0.0
        ) or 0.0
        return max(material_cost + operation_cost, 0.0)

    def _get_line_exclusion_reason(
        self,
        invoice_line,
        attribution,
        sale_lines,
        sale_orders,
        revenue_amount,
        cash_collected,
    ):
        product = invoice_line.product_id
        product_template = product.product_tmpl_id if product else self.env['product.template']

        if not product:
            return 'missing_product'
        if len(sale_orders) > 1:
            return 'multiple_sales_orders'
        if not sale_lines:
            return 'missing_sale_line'
        if len(sale_lines) > 1:
            return 'ambiguous_sale_line'
        if not attribution.get('salesperson'):
            return 'missing_account_salesperson'
        if product_template.unico_commission_gp_excluded:
            return 'product_excluded'
        if sale_lines.filtered('is_downpayment'):
            return 'down_payment'
        if not revenue_amount:
            return 'zero_revenue'
        if self._get_sale_line_unit_cost(sale_lines[:1]) <= 0.0:
            return 'missing_cost'
        if not cash_collected:
            return 'pending_collection'
        return 'none'

    def _get_line_cost_amount(self, invoice_line, sale_lines, direction):
        if len(sale_lines) != 1:
            return 0.0
        sale_line = sale_lines[:1]
        unit_cost = self._get_sale_line_unit_cost(sale_line)
        if unit_cost <= 0.0:
            return 0.0
        quantity = invoice_line.quantity or 0.0
        if invoice_line.product_uom_id and sale_line.product_uom_id:
            quantity = invoice_line.product_uom_id._compute_quantity(quantity, sale_line.product_uom_id)
        cost_amount = unit_cost * quantity
        cost_amount = self._convert_amount_to_period_currency(
            cost_amount,
            sale_line.currency_id,
            invoice_line.move_id.invoice_date or invoice_line.move_id.date,
        )
        return direction * cost_amount

    def _get_margin_protection_values(self, margin_percent, has_margin_basis):
        self.ensure_one()
        if not has_margin_basis:
            return 'not_applicable', 0.0
        if float_compare(
            margin_percent,
            self.margin_full_payout_threshold,
            precision_digits=6,
        ) >= 0:
            return 'full_payout', self._get_default_margin_payout_factor('full_payout')
        if float_compare(
            margin_percent,
            self.margin_reduced_75_threshold,
            precision_digits=6,
        ) >= 0:
            return 'reduced_75', self._get_default_margin_payout_factor('reduced_75')
        if float_compare(
            margin_percent,
            self.margin_reduced_50_threshold,
            precision_digits=6,
        ) >= 0:
            return 'reduced_50', self._get_default_margin_payout_factor('reduced_50')
        return 'no_payout', 0.0

    def _get_default_margin_payout_factor(self, band):
        self.ensure_one()
        entry = MARGIN_BAND_FACTORS.get(band)
        if entry:
            field, default, __label = entry
            return self[field] or default
        return 0.0

    def _get_line_collection_values(self, move):
        self.ensure_one()
        collection_date = move.unico_commission_collection_date
        collected_in_period = bool(
            move.unico_commission_cash_collected
            and collection_date
            and self.date_start <= collection_date <= self.date_end
        )
        return {
            'payment_state': move.payment_state,
            'collection_date': collection_date,
            'cash_collected': collected_in_period,
        }

    def _prepare_gp_line_vals(self, invoice_line, override_vals=None):
        self.ensure_one()
        override_vals = override_vals or {}
        move = invoice_line.move_id
        direction = self._get_line_direction(invoice_line)
        sale_lines = invoice_line.sale_line_ids.filtered(lambda line: not line.display_type)
        sale_orders = sale_lines.order_id
        attribution = self._get_line_attribution(invoice_line)
        salesperson = attribution.get('salesperson')
        collection = self._get_line_collection_values(move)
        revenue_amount = self._convert_amount_to_period_currency(
            invoice_line.price_subtotal or 0.0,
            invoice_line.currency_id or move.currency_id,
            move.invoice_date or move.date,
        )
        revenue_amount *= direction
        cost_amount = self._get_line_cost_amount(invoice_line, sale_lines, direction)
        effective_cash_collected = (
            collection.get('cash_collected') or not self.require_cash_collected
        )
        auto_exclusion_reason = self._get_line_exclusion_reason(
            invoice_line,
            attribution,
            sale_lines,
            sale_orders,
            revenue_amount,
            effective_cash_collected,
        )
        if len(sale_lines) != 1 or auto_exclusion_reason == 'missing_cost':
            cost_amount = 0.0
            gross_profit_amount = 0.0
            margin_percent = 0.0
        else:
            gross_profit_amount = revenue_amount - cost_amount
            margin_percent = gross_profit_amount / revenue_amount if revenue_amount else 0.0
        margin_protection_band, policy_margin_payout_factor = self._get_margin_protection_values(
            margin_percent,
            len(sale_lines) == 1 and auto_exclusion_reason not in ('missing_cost', 'zero_revenue'),
        )
        if auto_exclusion_reason == 'none' and margin_protection_band == 'no_payout':
            auto_exclusion_reason = 'low_margin_no_payout'

        values = {
            'period_id': self.id,
            'account_move_line_id': invoice_line.id,
            'move_id': move.id,
            'invoice_date': move.invoice_date,
            'invoice_payment_state': collection.get('payment_state'),
            'collection_date': collection.get('collection_date'),
            'cash_collected': collection.get('cash_collected'),
            'move_type': move.move_type,
            'is_credit': move.move_type == 'out_refund',
            'partner_id': move.partner_id.id,
            'commercial_partner_id': move.commercial_partner_id.id,
            'account_salesperson_id': attribution.get('account_salesperson').id if attribution.get('account_salesperson') else False,
            'salesperson_id': salesperson.id if salesperson else False,
            'attribution_partner_id': attribution.get('partner').id if attribution.get('partner') else False,
            'attribution_source': attribution.get('source') or 'missing_account_salesperson',
            'attribution_snapshot_date': attribution.get('snapshot_date'),
            'attribution_snapshot_user_id': attribution.get('snapshot_user').id if attribution.get('snapshot_user') else False,
            'attribution_warning': attribution.get('warning') or 'none',
            'product_id': invoice_line.product_id.id,
            'product_categ_id': invoice_line.product_id.categ_id.id,
            'sale_order_ids': [(6, 0, sale_orders.ids)],
            'sale_order_id': sale_orders[:1].id if len(sale_orders) == 1 else False,
            'sale_order_line_id': sale_lines[:1].id if len(sale_lines) == 1 else False,
            'quantity': direction * (invoice_line.quantity or 0.0),
            'revenue_amount': revenue_amount,
            'cost_amount': cost_amount,
            'gross_profit_amount': gross_profit_amount,
            'margin_percent': margin_percent,
            'margin_protection_band': margin_protection_band,
            'policy_margin_payout_factor': policy_margin_payout_factor,
            'auto_commissionable': auto_exclusion_reason == 'none',
            'auto_exclusion_reason': auto_exclusion_reason,
        }
        values.update(override_vals)
        if values.get('manual_commission_state') == 'reassign' and values.get('manual_salesperson_id'):
            values['salesperson_id'] = values['manual_salesperson_id']
        return values

    def action_calculate(self):
        for period in self:
            period._check_manager_access()
            if period.state not in ('draft', 'ready_for_review'):
                raise UserError(_("Only draft or ready-for-review GP reporting periods can be recalculated."))
            if period.state == 'ready_for_review':
                period.state = 'draft'
                period.message_post(body=_(
                    "Period moved back to Draft for recalculation. Existing manual overrides "
                    "are preserved for matching source lines."
                ))

            period._snapshot_commission_plan()

            preserved = {
                line.account_move_line_id.id: {
                    'manual_commission_state': line.manual_commission_state,
                    'manual_exclusion_reason': (
                        line.manual_exclusion_reason
                        or ('manager_excluded' if line.manual_commission_state == 'exclude' else False)
                    ),
                    'manual_salesperson_id': line.manual_salesperson_id.id,
                    'manager_note': line.manager_note,
                    'override_user_id': line.override_user_id.id,
                    'override_date': line.override_date,
                    'salesperson_id': (
                        line.salesperson_id.id
                        if line.manual_commission_state == 'reassign'
                        else line.account_salesperson_id.id
                    ),
                }
                for line in period.line_ids
            }
            # Managers do not have general unlink access on GP lines; the
            # controlled draft recalculation path is the only place generated
            # lines should be removed and rebuilt.
            period.line_ids.sudo().unlink()

            invoice_lines = self.env['account.move.line'].search(period._get_invoice_line_domain())
            vals_list = []
            for invoice_line in invoice_lines:
                vals = period._prepare_gp_line_vals(
                    invoice_line,
                    preserved.get(invoice_line.id),
                )
                if period.salesperson_id and vals.get('salesperson_id') != period.salesperson_id.id:
                    continue
                vals_list.append(vals)
            created_lines = self.env['unico.commission.gp.line']
            if vals_list:
                created_lines = self.env['unico.commission.gp.line'].create(vals_list)
            period._sync_salesperson_summaries()
            commissionable_count = len(created_lines.filtered('commissionable'))
            period.message_post(body=_(
                "Calculated %(line_count)s invoice lines: %(commissionable_count)s "
                "commissionable, %(exception_count)s exceptions.",
                line_count=len(created_lines),
                commissionable_count=commissionable_count,
                exception_count=len(created_lines) - commissionable_count,
            ))
            period.last_calculated_date = fields.Datetime.now()

        return True

    def _snapshot_commission_plan(self):
        self.ensure_one()
        plan = self.plan_id
        if not plan:
            return
        self.write({
            'margin_full_payout_threshold': plan.margin_full_payout_threshold,
            'margin_reduced_75_threshold': plan.margin_reduced_75_threshold,
            'margin_reduced_50_threshold': plan.margin_reduced_50_threshold,
            'margin_full_payout_factor': plan.margin_full_payout_factor or DEFAULT_FULL_PAYOUT_FACTOR,
            'margin_reduced_75_payout_factor': plan.margin_reduced_75_payout_factor,
            'margin_reduced_50_payout_factor': plan.margin_reduced_50_payout_factor,
            'default_commission_rate': plan.default_commission_rate,
            'require_cash_collected': plan.require_cash_collected,
            'attribution_basis': plan.attribution_basis,
        })

    def _sync_salesperson_summaries(self):
        summary_model = self.env['unico.commission.gp.salesperson.summary']
        line_model = self.env['unico.commission.gp.line']
        adjustment_model = self.env['unico.commission.gp.adjustment']
        for period in self:
            existing_by_salesperson = {
                summary.salesperson_id.id: summary
                for summary in period.summary_ids
            }
            line_groups = line_model._read_group(
                [('period_id', '=', period.id), ('salesperson_id', '!=', False)],
                ['salesperson_id'],
                ['__count', 'revenue_amount:sum', 'gross_profit_amount:sum', 'commission_basis_amount:sum'],
            )
            line_totals_by_salesperson = {
                salesperson.id: {
                    'line_count': line_count,
                    'revenue_amount': revenue_amount or 0.0,
                    'gross_profit_amount': gross_profit_amount or 0.0,
                    'line_commission_basis': line_commission_basis or 0.0,
                }
                for salesperson, line_count, revenue_amount, gross_profit_amount, line_commission_basis in line_groups
            }
            exception_counts = {
                salesperson.id: count
                for salesperson, count in line_model._read_group(
                    [
                        ('period_id', '=', period.id),
                        ('salesperson_id', '!=', False),
                        ('commissionable', '=', False),
                    ],
                    ['salesperson_id'],
                    ['__count'],
                )
            }
            basis_adjustments = {
                salesperson.id: amount
                for salesperson, amount in adjustment_model._read_group(
                    [('period_id', '=', period.id), ('salesperson_id', '!=', False)],
                    ['salesperson_id'],
                    ['amount:sum'],
                )
            }
            target_salesperson_ids = set(line_totals_by_salesperson) | set(basis_adjustments)
            for salesperson_id in target_salesperson_ids:
                summary = existing_by_salesperson.get(salesperson_id)
                commission_rate = summary.commission_rate if summary else period.default_commission_rate
                manual_adjustment = summary.manual_commission_adjustment_amount if summary else 0.0
                grouped = line_totals_by_salesperson.get(salesperson_id, {
                    'line_count': 0,
                    'revenue_amount': 0.0,
                    'gross_profit_amount': 0.0,
                    'line_commission_basis': 0.0,
                })
                basis_adjustment = basis_adjustments.get(salesperson_id, 0.0) or 0.0
                commission_basis = grouped['line_commission_basis'] + basis_adjustment
                values = {
                    'period_id': period.id,
                    'salesperson_id': salesperson_id,
                    'company_id': period.company_id.id,
                    'line_count': grouped['line_count'],
                    'exception_line_count': exception_counts.get(salesperson_id, 0),
                    'revenue_amount': grouped['revenue_amount'],
                    'gross_profit_amount': grouped['gross_profit_amount'],
                    'gp_basis_adjustment_amount': basis_adjustment,
                    'commission_basis_amount': commission_basis,
                    'commission_rate': commission_rate,
                    'manual_commission_adjustment_amount': manual_adjustment,
                }
                for band, (field, __default, __label) in MARGIN_BAND_FACTORS.items():
                    values[field] = (
                        summary[field]
                        if summary and summary[field]
                        else period._get_default_margin_payout_factor(band)
                    )
                if summary:
                    summary.with_context(skip_commission_summary_line_recompute=True).write(values)
                else:
                    summary_model.create(values)
            obsolete = period.summary_ids.filtered(lambda summary: summary.salesperson_id.id not in target_salesperson_ids)
            # Same controlled-rebuild rule as transaction lines: managers can
            # refresh draft reports without broad manual delete rights.
            obsolete.sudo().unlink()

    def _check_period_was_calculated(self):
        uncalculated = self.filtered(lambda period: not period.last_calculated_date)
        if uncalculated:
            raise UserError(_("Calculate the commission period before submitting, approving, or exporting it."))

    def _check_commission_summary_consistency(self):
        for period in self:
            if period.state == 'draft':
                period._sync_salesperson_summaries()
            rounding = period.currency_id.rounding
            summary_basis = sum(period.summary_ids.mapped('commission_basis_amount'))
            if float_compare(summary_basis, period.adjusted_commissionable_gp_amount, precision_rounding=rounding):
                raise UserError(_(
                    "Salesperson commission basis does not match the period total. "
                    "Every GP basis line and every GP basis adjustment must name a "
                    "commission salesperson before this period can be locked."
                ))

    def action_submit_for_review(self):
        for period in self:
            period._check_manager_access()
            period._check_period_was_calculated()
            period._check_commission_summary_consistency()
            if period.state == 'draft':
                period.state = 'ready_for_review'
        return True

    def action_approve(self):
        for period in self:
            period._check_manager_access()
            period._check_period_was_calculated()
            period._check_commission_summary_consistency()
            if period.state == 'ready_for_review':
                period.state = 'approved'
        return True

    def action_export_payroll(self):
        self.ensure_one()
        self._check_manager_access()
        self._check_period_was_calculated()
        self._check_commission_summary_consistency()
        if self.state != 'approved':
            raise UserError(_("Only approved periods can be exported to payroll."))
        self.state = 'exported'
        self.exported_date = fields.Datetime.now()
        self.exported_by_id = self.env.user
        return self._action_download_payroll_csv()

    def action_reopen(self):
        for period in self:
            period._check_manager_access()
            if period.state == 'exported':
                raise UserError(_("Exported periods are final and cannot be reopened."))
            if (
                period.state == 'approved'
                and not self.env.user.has_group('unico_commission_gp_reporting.group_unico_commission_gp_admin')
            ):
                raise AccessError(_("Only commission GP administrators can reopen approved periods."))
            if period.state in ('ready_for_review', 'approved'):
                period.state = 'draft'
        return True

    def action_cancel(self):
        for period in self:
            period._check_manager_access()
            period.state = 'cancelled'
        return True

    def action_review_exceptions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'unico_commission_gp_reporting.action_unico_commission_exceptions'
        )
        action['domain'] = [('period_id', '=', self.id), ('commissionable', '=', False)]
        action['context'] = {'default_period_id': self.id}
        return action

    def _action_download_payroll_csv(self):
        self.ensure_one()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['Salesperson', 'Commission Basis', 'Rate', 'Calculated', 'Adjustment', 'Final Payable'])
        total_calc = total_adj = total_final = 0.0
        for summary in self.summary_ids.sorted(key=lambda s: s.salesperson_id.name or ''):
            writer.writerow([
                summary.salesperson_id.name or '',
                '%.2f' % summary.commission_basis_amount,
                '%.4f' % summary.commission_rate,
                '%.2f' % summary.calculated_commission_amount,
                '%.2f' % summary.manual_commission_adjustment_amount,
                '%.2f' % summary.final_approved_commission_amount,
            ])
            total_calc += summary.calculated_commission_amount
            total_adj += summary.manual_commission_adjustment_amount
            total_final += summary.final_approved_commission_amount
        writer.writerow(['TOTAL', '', '', '%.2f' % total_calc, '%.2f' % total_adj, '%.2f' % total_final])

        filename = 'commission_payroll_%s.csv' % (self.name or self.id)
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(buffer.getvalue().encode('utf-8')),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'text/csv',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'self',
            'res_id': attachment.id,
        }

    def write(self, vals):
        protected_fields = {
            'date_start',
            'date_end',
            'salesperson_id',
            'company_id',
            'plan_id',
            'require_cash_collected',
            'attribution_basis',
            'default_commission_rate',
            'margin_full_payout_threshold',
            'margin_reduced_75_threshold',
            'margin_reduced_50_threshold',
            'margin_full_payout_factor',
            'margin_reduced_75_payout_factor',
            'margin_reduced_50_payout_factor',
        }
        if protected_fields & set(vals):
            locked = self.filtered(lambda period: period.state != 'draft')
            if locked:
                raise UserError(_("Reopen the GP reporting period before changing its scope."))
        return super().write(vals)


class UnicoCommissionGpLine(models.Model):
    _name = 'unico.commission.gp.line'
    _description = 'Commissionable GP Transaction Line'
    _order = 'invoice_date desc, id desc'
    _rec_names_search = ['move_id', 'partner_id', 'product_id']

    period_id = fields.Many2one(
        'unico.commission.gp.period',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(related='period_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='period_id.currency_id', store=True, readonly=True)
    account_move_line_id = fields.Many2one(
        'account.move.line',
        string='Invoice Line',
        required=True,
        # Preserve the audit trail from closed GP reports back to their source invoice lines.
        ondelete='restrict',
        index=True,
    )
    move_id = fields.Many2one('account.move', string='Invoice / Credit Note', required=True, index=True)
    invoice_date = fields.Date(required=True, index=True)
    invoice_payment_state = fields.Selection(
        INVOICE_PAYMENT_STATES,
        string='Payment Status',
        readonly=True,
        index=True,
        help="Snapshot from the source invoice at calculation time. Recalculate the period to refresh it.",
    )
    collection_date = fields.Date(
        string='Collection Date',
        readonly=True,
        index=True,
        help="Latest cash, bank, or payment date when the source invoice/refund became commission eligible.",
    )
    cash_collected = fields.Boolean(
        string='Cash Collected In Period',
        readonly=True,
        index=True,
        help="True only when actual cash, bank, or payment collection occurred inside this commission period.",
    )
    move_type = fields.Selection(
        [
            ('out_invoice', 'Customer Invoice'),
            ('out_refund', 'Customer Credit Note'),
        ],
        required=True,
    )
    is_credit = fields.Boolean(string='Credit / Return')
    partner_id = fields.Many2one('res.partner', string='Customer', index=True)
    commercial_partner_id = fields.Many2one('res.partner', string='Commercial Customer', index=True)
    account_salesperson_id = fields.Many2one(
        'res.users',
        string='Account Salesperson Snapshot',
        readonly=True,
        index=True,
    )
    salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Salesperson',
        index=True,
        help="Effective commission owner. Normally the customer account owner; managers can reassign with an audited override.",
    )
    attribution_partner_id = fields.Many2one(
        'res.partner',
        string='Attribution Account',
        readonly=True,
        index=True,
    )
    attribution_source = fields.Selection(
        [
            ('account_owner', 'Account Salesperson'),
            ('sale_order_snapshot', 'Sales Order Snapshot'),
            ('invoice_snapshot', 'Invoice Snapshot'),
            ('current_account_owner', 'Current Account Salesperson'),
            ('sales_order_user', 'Sales Order Salesperson'),
            ('invoice_user', 'Invoice Salesperson'),
            ('missing_account_salesperson', 'Missing Account Salesperson'),
            ('multiple_sales_orders', 'Multiple Sales Orders'),
        ],
        string='Attribution Source',
        default='missing_account_salesperson',
        readonly=True,
    )
    attribution_snapshot_date = fields.Datetime(string='Attribution Snapshot Date', readonly=True)
    attribution_snapshot_user_id = fields.Many2one('res.users', string='Attribution Snapshotted By', readonly=True)
    attribution_warning = fields.Selection(
        [
            ('none', 'None'),
            ('missing_snapshot', 'Missing Attribution Snapshot'),
            ('account_salesperson_changed', 'Account Salesperson Changed After Order'),
        ],
        default='none',
        readonly=True,
    )
    product_id = fields.Many2one('product.product', index=True)
    product_categ_id = fields.Many2one('product.category', string='Product Category', index=True)
    sale_order_ids = fields.Many2many(
        'sale.order',
        'unico_commission_gp_line_sale_order_rel',
        'line_id',
        'order_id',
        string='Sales Orders',
        readonly=True,
    )
    sale_order_id = fields.Many2one('sale.order', string='Primary Sales Order', index=True)
    sale_order_line_id = fields.Many2one('sale.order.line', string='Sales Order Line', index=True)
    quantity = fields.Float(digits='Product Unit of Measure')
    revenue_amount = fields.Monetary(string='Revenue')
    cost_amount = fields.Monetary(string='Cost')
    gross_profit_amount = fields.Monetary(string='Gross Profit')
    margin_percent = fields.Float(string='Margin %', aggregator='avg')
    margin_protection_band = fields.Selection(
        [
            ('full_payout', 'Full Band'),
            ('reduced_75', 'Reduced Band'),
            ('reduced_50', 'Low Margin Band'),
            ('no_payout', 'No Payout'),
            ('not_applicable', 'Not Applicable'),
        ],
        default='not_applicable',
        required=True,
        readonly=True,
        index=True,
        string='Payout Band',
    )
    policy_margin_payout_factor = fields.Float(
        string='Policy GP Eligible %',
        readonly=True,
        aggregator='avg',
        help="Percent of GP eligible from the period policy before salesperson-specific overrides.",
    )
    margin_payout_factor = fields.Float(
        string='Payout %',
        compute='_compute_effective_margin_payout',
        store=True,
        readonly=True,
        aggregator='avg',
        help=(
            "Effective percent of GP eligible after salesperson-specific overrides. "
            "Manual Include and Reassign use that salesperson's full-band eligible percent."
        ),
    )
    margin_adjusted_gp_amount = fields.Monetary(
        string='GP After Margin Protection',
        compute='_compute_effective_margin_payout',
        store=True,
        readonly=True,
        help=(
            "Gross profit after applying the effective margin protection payout factor. "
            "Excluded lines show this for reference but do not contribute to commissionable totals."
        ),
    )
    auto_commissionable = fields.Boolean(readonly=True)
    auto_exclusion_reason = fields.Selection(
        [
            ('none', 'None'),
            ('missing_product', 'Missing Product'),
            ('missing_salesperson', 'Missing Salesperson (Legacy)'),
            ('missing_account_salesperson', 'Missing Account Salesperson'),
            ('ambiguous_salesperson', 'Multiple Salespeople (Legacy)'),
            ('multiple_sales_orders', 'Multiple Sales Orders'),
            ('product_excluded', 'Product Excluded'),
            ('down_payment', 'Down Payment'),
            ('missing_sale_line', 'Missing Sales Order Line'),
            ('ambiguous_sale_line', 'Multiple Sales Order Lines'),
            ('pending_collection', 'Pending Collection'),
            ('missing_cost', 'Missing Cost'),
            ('zero_revenue', 'Zero Revenue'),
            ('low_margin_no_payout', 'Low Margin - No Payout'),
        ],
        default='none',
        required=True,
        readonly=True,
    )
    manual_commission_state = fields.Selection(
        [
            ('auto', 'Use Automatic Classification'),
            ('include', 'Include'),
            ('exclude', 'Exclude'),
            ('reassign', 'Reassign'),
            ('split', 'Split Commission Required'),
        ],
        default='auto',
        required=True,
        string='Manager Override',
    )
    manual_exclusion_reason = fields.Selection(
        [
            ('manager_excluded', 'Manager Excluded'),
            ('manager_override', 'Manager Override'),
            ('reassigned', 'Reassigned'),
            ('split_commission', 'Split Commission Handled Manually'),
            ('special_case', 'Special Case'),
            ('other', 'Other'),
        ],
        string='Override Reason',
    )
    manual_salesperson_id = fields.Many2one(
        'res.users',
        string='Reassigned Commission Salesperson',
        help="Required when the manager override is Reassign.",
    )
    manager_note = fields.Text()
    override_user_id = fields.Many2one('res.users', string='Override User', readonly=True)
    override_date = fields.Datetime(string='Override Date', readonly=True)
    commissionable = fields.Boolean(compute='_compute_effective_classification', store=True)
    effective_exclusion_reason = fields.Selection(
        [
            ('none', 'None'),
            ('missing_product', 'Missing Product'),
            ('missing_salesperson', 'Missing Salesperson (Legacy)'),
            ('missing_account_salesperson', 'Missing Account Salesperson'),
            ('ambiguous_salesperson', 'Multiple Salespeople (Legacy)'),
            ('multiple_sales_orders', 'Multiple Sales Orders'),
            ('product_excluded', 'Product Excluded'),
            ('down_payment', 'Down Payment'),
            ('missing_sale_line', 'Missing Sales Order Line'),
            ('ambiguous_sale_line', 'Multiple Sales Order Lines'),
            ('pending_collection', 'Pending Collection'),
            ('missing_cost', 'Missing Cost'),
            ('zero_revenue', 'Zero Revenue'),
            ('low_margin_no_payout', 'Low Margin - No Payout'),
            ('account_salesperson_changed', 'Account Salesperson Changed After Order'),
            ('manager_excluded', 'Manager Excluded'),
            ('manager_override', 'Manager Override'),
            ('reassigned', 'Reassigned'),
            ('split_commission', 'Split Commission Handled Manually'),
            ('special_case', 'Special Case'),
            ('other', 'Other'),
        ],
        string='Why Excluded',
        compute='_compute_effective_classification',
        store=True,
    )
    commission_basis_amount = fields.Monetary(
        string='Commission Basis',
        compute='_compute_commission_amounts',
        store=True,
    )
    commission_rate = fields.Float(
        string='Commission Rate',
        compute='_compute_commission_amounts',
        store=True,
        readonly=True,
        aggregator='avg',
        help="Derived from the salesperson summary rate. Edit the summary rate to change payout.",
    )
    calculated_commission_amount = fields.Monetary(
        string='Commission Amount',
        compute='_compute_commission_amounts',
        store=True,
    )

    _period_move_line_unique = models.Constraint(
        'UNIQUE(period_id, account_move_line_id)',
        'Each invoice line can appear only once in a GP reporting period.',
    )

    @api.depends('auto_commissionable', 'auto_exclusion_reason', 'manual_commission_state', 'manual_exclusion_reason')
    def _compute_effective_classification(self):
        for line in self:
            if line.manual_commission_state in ('include', 'reassign'):
                line.commissionable = True
                line.effective_exclusion_reason = 'none'
            elif line.manual_commission_state == 'exclude':
                line.commissionable = False
                line.effective_exclusion_reason = line.manual_exclusion_reason or 'manager_excluded'
            elif line.manual_commission_state == 'split':
                line.commissionable = False
                line.effective_exclusion_reason = line.manual_exclusion_reason or 'split_commission'
            else:
                line.commissionable = line.auto_commissionable
                line.effective_exclusion_reason = line.auto_exclusion_reason if not line.auto_commissionable else 'none'

    @api.depends(
        'gross_profit_amount',
        'manual_commission_state',
        'margin_protection_band',
        'policy_margin_payout_factor',
        'period_id.margin_full_payout_factor',
        'period_id.margin_reduced_75_payout_factor',
        'period_id.margin_reduced_50_payout_factor',
        'period_id.summary_ids.salesperson_id',
        'period_id.summary_ids.margin_full_payout_factor',
        'period_id.summary_ids.margin_reduced_75_payout_factor',
        'period_id.summary_ids.margin_reduced_50_payout_factor',
        'salesperson_id',
    )
    def _compute_effective_margin_payout(self):
        summary_by_period_salesperson = {
            (summary.period_id.id, summary.salesperson_id.id): summary
            for summary in self.mapped('period_id.summary_ids')
        }
        for line in self:
            summary = summary_by_period_salesperson.get((line.period_id.id, line.salesperson_id.id))
            if line.manual_commission_state in ('include', 'reassign'):
                band = 'full_payout'
            else:
                band = line.margin_protection_band
            line.margin_payout_factor = line._get_effective_margin_payout_factor(summary, band)
            line.margin_adjusted_gp_amount = line.gross_profit_amount * line.margin_payout_factor

    def _get_effective_margin_payout_factor(self, summary, band):
        self.ensure_one()
        entry = MARGIN_BAND_FACTORS.get(band)
        if entry:
            field, __default, __label = entry
            if summary and summary[field]:
                return summary[field]
            return self.period_id._get_default_margin_payout_factor(band)
        return self.policy_margin_payout_factor or 0.0

    @api.depends(
        'commissionable',
        'margin_adjusted_gp_amount',
        'period_id.default_commission_rate',
        'period_id.summary_ids.commission_rate',
        'salesperson_id',
    )
    def _compute_commission_amounts(self):
        summary_by_period_salesperson = {
            (summary.period_id.id, summary.salesperson_id.id): summary
            for summary in self.mapped('period_id.summary_ids')
        }
        for line in self:
            summary = summary_by_period_salesperson.get((line.period_id.id, line.salesperson_id.id))
            line.commission_rate = summary.commission_rate if summary else line.period_id.default_commission_rate
            line.commission_basis_amount = line.margin_adjusted_gp_amount if line.commissionable else 0.0
            line.calculated_commission_amount = line.commission_basis_amount * (line.commission_rate or 0.0)

    @api.constrains('manual_commission_state', 'manual_exclusion_reason', 'manager_note', 'manual_salesperson_id')
    def _check_manual_exclusion_reason(self):
        for line in self:
            if line.manual_commission_state != 'auto' and not line.manual_exclusion_reason:
                raise ValidationError(_("Choose an override reason before changing a GP line classification."))
            if line.manual_commission_state != 'auto' and not line.manager_note:
                raise ValidationError(_("Enter a manager note before changing a GP line classification."))
            if line.manual_commission_state == 'reassign' and not line.manual_salesperson_id:
                raise ValidationError(_("Choose the reassigned commission salesperson before reassigning a GP line."))
            if line.manual_commission_state == 'include' and not line.salesperson_id:
                raise ValidationError(_(
                    "Included commission lines must name a commission salesperson. "
                    "Use the Reassign override when ownership is unclear."
                ))

    def _check_period_editable(self):
        locked = self.filtered(lambda line: line.period_id.state != 'draft')
        if locked:
            raise UserError(_("Reopen the GP reporting period before changing its lines."))

    def write(self, vals):
        self._check_period_editable()
        vals = dict(vals)
        override_fields = {
            'manual_commission_state',
            'manual_exclusion_reason',
            'manual_salesperson_id',
            'manager_note',
        }
        if override_fields & set(vals) and not self.env.context.get('skip_unico_commission_override_audit'):
            vals.setdefault('override_user_id', self.env.user.id)
            vals.setdefault('override_date', fields.Datetime.now())
        if vals.get('manual_commission_state') == 'auto':
            periods = self.mapped('period_id')
            lines_by_salesperson = {}
            for line in self:
                salesperson_id = line.account_salesperson_id.id or False
                lines_by_salesperson.setdefault(salesperson_id, self.env['unico.commission.gp.line'])
                lines_by_salesperson[salesperson_id] |= line
            for salesperson_id, lines in lines_by_salesperson.items():
                super(UnicoCommissionGpLine, lines).write({
                    **vals,
                    'salesperson_id': salesperson_id,
                })
            periods._sync_salesperson_summaries()
            return True
        if (
            vals.get('manual_commission_state') == 'reassign'
            or ('manual_salesperson_id' in vals and any(line.manual_commission_state == 'reassign' for line in self))
        ):
            reassigned_salesperson_id = vals.get('manual_salesperson_id')
            if reassigned_salesperson_id:
                vals.setdefault('salesperson_id', reassigned_salesperson_id)
        if 'salesperson_id' in vals and not self.env.context.get('skip_unico_commission_salesperson_audit'):
            valid_reassign = (
                vals.get('salesperson_id')
                and vals.get('manual_salesperson_id') == vals.get('salesperson_id')
                and (
                    vals.get('manual_commission_state') == 'reassign'
                    or (
                        'manual_salesperson_id' in vals
                        and all(line.manual_commission_state == 'reassign' for line in self)
                    )
                )
            )
            if not valid_reassign:
                raise ValidationError(_(
                    "Use the Reassign manager override to change a commission salesperson."
                ))
        res = super().write(vals)
        if override_fields & set(vals):
            self.mapped('period_id')._sync_salesperson_summaries()
        return res

    def unlink(self):
        self._check_period_editable()
        return super().unlink()

    def action_view_invoice(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoice'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    def action_view_sale_order(self):
        self.ensure_one()
        if not self.sale_order_id:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sales Order'),
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': self.sale_order_id.id,
        }


class UnicoCommissionGpSalespersonSummary(models.Model):
    _name = 'unico.commission.gp.salesperson.summary'
    _description = 'Commission GP Salesperson Summary'
    _order = 'period_id desc, salesperson_id'

    period_id = fields.Many2one(
        'unico.commission.gp.period',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one('res.company', required=True, index=True)
    currency_id = fields.Many2one(related='period_id.currency_id', store=True, readonly=True)
    state = fields.Selection(related='period_id.state', store=True, readonly=True)
    salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Salesperson',
        required=True,
        index=True,
    )
    line_count = fields.Integer(readonly=True)
    exception_line_count = fields.Integer(string='Excluded / Exception Lines', readonly=True)
    revenue_amount = fields.Monetary(string='Revenue', readonly=True)
    gross_profit_amount = fields.Monetary(string='Gross Profit', readonly=True)
    gp_basis_adjustment_amount = fields.Monetary(
        string='GP Basis Adjustment',
        readonly=True,
        help="Salesperson-specific GP basis adjustments from the GP Basis Adjustments tab.",
    )
    commission_basis_amount = fields.Monetary(string='Commission Basis', readonly=True)
    commission_rate = fields.Float(
        string='Commission Rate',
        aggregator='avg',
        help="Enter as a decimal. 0.10 means 10%.",
    )
    margin_full_payout_factor = fields.Float(
        string='Full Band GP Eligible',
        default=DEFAULT_FULL_PAYOUT_FACTOR,
        aggregator='avg',
        help=(
            "Percent of this salesperson's full-band GP that is eligible for commission. "
            "This is not the commission rate; 1.00 means 100% of GP is eligible."
        ),
    )
    margin_reduced_75_payout_factor = fields.Float(
        string='Reduced Band GP Eligible',
        default=DEFAULT_REDUCED_75_PAYOUT_FACTOR,
        aggregator='avg',
        help=(
            "Percent of this salesperson's reduced-band GP that is eligible for commission. "
            "This is not the commission rate; 0.75 means 75% of GP is eligible."
        ),
    )
    margin_reduced_50_payout_factor = fields.Float(
        string='Low Margin Band GP Eligible',
        default=DEFAULT_REDUCED_50_PAYOUT_FACTOR,
        aggregator='avg',
        help=(
            "Percent of this salesperson's low-margin-band GP that is eligible for commission. "
            "This is not the commission rate; 0.50 means 50% of GP is eligible."
        ),
    )
    calculated_commission_amount = fields.Monetary(
        string='Calculated Commission Amount',
        compute='_compute_summary_commission',
        store=True,
        readonly=True,
    )
    manual_commission_adjustment_amount = fields.Monetary(string='Manual Commission Adjustment')
    final_approved_commission_amount = fields.Monetary(
        string='Final Approved Commission Amount',
        compute='_compute_summary_commission',
        store=True,
        readonly=True,
    )

    _period_salesperson_unique = models.Constraint(
        'UNIQUE(period_id, salesperson_id)',
        'Each salesperson can appear only once in a GP reporting period summary.',
    )

    def _check_period_editable(self):
        locked = self.filtered(lambda summary: summary.period_id.state != 'draft')
        if locked:
            raise UserError(_("Reopen the GP reporting period before changing salesperson commission summaries."))

    @api.constrains('commission_rate')
    def _check_commission_rate(self):
        for summary in self:
            if not 0.0 <= (summary.commission_rate or 0.0) <= 1.0:
                raise ValidationError(_(
                    "Commission rate must be between 0.00 and 1.00. Enter 0.10 for 10%."
                ))

    @api.constrains(*MARGIN_BAND_FACTOR_FIELDS)
    def _check_margin_payout_factors(self):
        for summary in self:
            for field, __default, label in MARGIN_BAND_FACTORS.values():
                value = summary[field] or 0.0
                if not 0.0 < value <= 1.0:
                    raise ValidationError(_(
                        "%(label)s must be greater than 0%% and no more than 100%%. Enter 0.75 for 75%%.",
                        label=_(label),
                    ))

    @api.depends('commission_basis_amount', 'commission_rate', 'manual_commission_adjustment_amount')
    def _compute_summary_commission(self):
        for summary in self:
            summary.calculated_commission_amount = (
                summary.commission_basis_amount * (summary.commission_rate or 0.0)
            )
            summary.final_approved_commission_amount = (
                summary.calculated_commission_amount + summary.manual_commission_adjustment_amount
            )

    def action_view_detail_lines(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'unico_commission_gp_reporting.action_unico_my_commission_lines'
        )
        action['domain'] = [
            ('period_id', '=', self.period_id.id),
            ('salesperson_id', '=', self.salesperson_id.id),
        ]
        action['context'] = {
            'search_default_group_exclusion': 0,
        }
        return action

    def write(self, vals):
        self._check_period_editable()
        res = super().write(vals)
        factor_fields = {
            'margin_full_payout_factor',
            'margin_reduced_75_payout_factor',
            'margin_reduced_50_payout_factor',
        }
        recompute_fields = {'commission_rate'} | factor_fields
        if (
            recompute_fields & set(vals)
            and not self.env.context.get('skip_commission_summary_line_recompute')
        ):
            lines = self.env['unico.commission.gp.line'].search([
                ('period_id', 'in', self.mapped('period_id').ids),
                ('salesperson_id', 'in', self.mapped('salesperson_id').ids),
            ])
            if factor_fields & set(vals):
                lines._compute_effective_margin_payout()
            lines._compute_commission_amounts()
            if factor_fields & set(vals):
                self.mapped('period_id').with_context(
                    skip_commission_summary_line_recompute=True,
                )._sync_salesperson_summaries()
        return res

    def unlink(self):
        self._check_period_editable()
        return super().unlink()


class UnicoCommissionGpAdjustment(models.Model):
    _name = 'unico.commission.gp.adjustment'
    _description = 'Commissionable GP Manual Adjustment'
    _order = 'date desc, id desc'

    period_id = fields.Many2one(
        'unico.commission.gp.period',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(related='period_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='period_id.currency_id', store=True, readonly=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    salesperson_id = fields.Many2one(
        'res.users',
        string='Commission Salesperson',
        help=(
            "Required for non-zero GP basis adjustments. Create one adjustment per "
            "salesperson so commission payout totals remain traceable."
        ),
    )
    amount = fields.Monetary(required=True)
    reason = fields.Selection(
        [
            ('credit_timing', 'Credit / Return Timing'),
            ('cost_correction', 'Cost Correction'),
            ('split_commission', 'Split Commission Handled Manually'),
            ('discretionary', 'Discretionary Management Adjustment'),
            ('other', 'Other'),
        ],
        required=True,
        default='other',
    )
    note = fields.Text(required=True)

    def _check_period_editable(self):
        locked = self.filtered(lambda adjustment: adjustment.period_id.state != 'draft')
        if locked:
            raise UserError(_("Reopen the GP reporting period before changing its adjustments."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('salesperson_id') or not vals.get('period_id'):
                continue
            period = self.env['unico.commission.gp.period'].browse(vals['period_id'])
            if period.salesperson_id:
                vals['salesperson_id'] = period.salesperson_id.id
        records = super().create(vals_list)
        records._check_period_editable()
        records.mapped('period_id')._sync_salesperson_summaries()
        return records

    @api.constrains('amount', 'salesperson_id')
    def _check_salesperson_required_for_basis_adjustment(self):
        for adjustment in self:
            if adjustment.currency_id:
                is_zero = float_is_zero(
                    adjustment.amount,
                    precision_rounding=adjustment.currency_id.rounding,
                )
            else:
                is_zero = not adjustment.amount
            if not is_zero and not adjustment.salesperson_id:
                raise ValidationError(_(
                    "GP basis adjustments must name a commission salesperson. "
                    "Create separate adjustment rows per salesperson."
                ))

    def write(self, vals):
        self._check_period_editable()
        res = super().write(vals)
        self.mapped('period_id')._sync_salesperson_summaries()
        return res

    def unlink(self):
        self._check_period_editable()
        periods = self.mapped('period_id')
        res = super().unlink()
        periods._sync_salesperson_summaries()
        return res
