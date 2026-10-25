# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

DEFAULT_FULL_PAYOUT_MARGIN_THRESHOLD = 0.28
DEFAULT_REDUCED_75_MARGIN_THRESHOLD = 0.25
DEFAULT_REDUCED_50_MARGIN_THRESHOLD = 0.20
DEFAULT_FULL_PAYOUT_FACTOR = 1.00
DEFAULT_REDUCED_75_PAYOUT_FACTOR = 0.75
DEFAULT_REDUCED_50_PAYOUT_FACTOR = 0.50

ATTRIBUTION_BASES = [
    ('account_owner', 'Customer Account Owner'),
    ('sales_order_user', 'Sales Order Salesperson'),
    ('invoice_user', 'Invoice Salesperson'),
]


class CommissionPlan(models.Model):
    _name = 'commission.plan'
    _description = 'Commission Plan'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    margin_full_payout_threshold = fields.Float(
        string='Full Band Starts At Margin',
        default=DEFAULT_FULL_PAYOUT_MARGIN_THRESHOLD,
        help="Margin percentage required for the full band. Stored as a decimal, so 0.28 means 28%.",
    )
    margin_reduced_75_threshold = fields.Float(
        string='Reduced Band Starts At Margin',
        default=DEFAULT_REDUCED_75_MARGIN_THRESHOLD,
        help="Minimum margin for the first reduced payout band. Stored as a decimal, so 0.25 means 25%.",
    )
    margin_reduced_50_threshold = fields.Float(
        string='Low Margin Band Starts At Margin',
        default=DEFAULT_REDUCED_50_MARGIN_THRESHOLD,
        help="Minimum margin for the low-margin payout band. Stored as a decimal, so 0.20 means 20%.",
    )
    margin_full_payout_factor = fields.Float(
        string='Full Band GP Eligible',
        default=DEFAULT_FULL_PAYOUT_FACTOR,
        help=(
            "Percent of gross profit eligible for commission in the full margin band. "
            "This is not the salesperson commission rate; 1.00 means 100% of GP is eligible."
        ),
    )
    margin_reduced_75_payout_factor = fields.Float(
        string='Reduced Band GP Eligible',
        default=DEFAULT_REDUCED_75_PAYOUT_FACTOR,
        help=(
            "Percent of gross profit eligible for commission in the reduced margin band. "
            "This is not the salesperson commission rate; 0.75 means 75% of GP is eligible."
        ),
    )
    margin_reduced_50_payout_factor = fields.Float(
        string='Low Margin Band GP Eligible',
        default=DEFAULT_REDUCED_50_PAYOUT_FACTOR,
        help=(
            "Percent of gross profit eligible for commission in the low-margin band. "
            "This is not the salesperson commission rate; 0.50 means 50% of GP is eligible."
        ),
    )
    default_commission_rate = fields.Float(
        string='Default Commission Rate',
        default=0.0,
        help="Enter as a decimal. 0.10 means 10%; 1.00 means 100%.",
    )
    require_cash_collected = fields.Boolean(
        string='Require Cash Collection', default=True,
        help="When enabled, commission is eligible only after the invoice is cash-collected.",
    )
    attribution_basis = fields.Selection(
        ATTRIBUTION_BASES, string='Attribution Basis',
        default='account_owner', required=True,
        help="Rule that decides which salesperson is credited for each commissionable line.",
    )

    def init(self):
        # Existing databases receive this column during module upgrade. Normalize
        # old plan rows so managers do not see a blank/zero full-band policy.
        self.search([
            '|',
            ('margin_full_payout_factor', '=', False),
            ('margin_full_payout_factor', '<=', 0.0),
        ]).write({'margin_full_payout_factor': DEFAULT_FULL_PAYOUT_FACTOR})

    @api.constrains(
        'margin_full_payout_threshold',
        'margin_reduced_75_threshold',
        'margin_reduced_50_threshold',
        'margin_full_payout_factor',
        'margin_reduced_75_payout_factor',
        'margin_reduced_50_payout_factor',
    )
    def _check_margin_protection_policy(self):
        for plan in self:
            if not (
                plan.margin_full_payout_threshold
                > plan.margin_reduced_75_threshold
                > plan.margin_reduced_50_threshold
                >= 0.0
            ):
                raise ValidationError(_(
                    "Margin protection thresholds must be ordered from highest to lowest: "
                    "full band, reduced band, then low margin band."
                ))
            if not 0.0 < plan.margin_full_payout_factor <= 1.0:
                raise ValidationError(_("The full-band GP eligible percent must be greater than 0% and no more than 100%."))
            if not 0.0 < plan.margin_reduced_75_payout_factor <= 1.0:
                raise ValidationError(_("The reduced-band GP eligible percent must be greater than 0% and no more than 100%."))
            if not 0.0 < plan.margin_reduced_50_payout_factor <= 1.0:
                raise ValidationError(_("The low-margin-band GP eligible percent must be greater than 0% and no more than 100%."))

    @api.constrains('default_commission_rate')
    def _check_default_commission_rate(self):
        for plan in self:
            if not 0.0 <= (plan.default_commission_rate or 0.0) <= 1.0:
                raise ValidationError(_(
                    "Default commission rate must be between 0.00 and 1.00. "
                    "Enter 0.10 for 10%, not 10."
                ))
