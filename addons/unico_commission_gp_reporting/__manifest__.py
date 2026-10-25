# -*- coding: utf-8 -*-
{
    'name': 'Unico Commissionable GP Reporting',
    'version': '19.0.1.0.1',
    'summary': 'Monthly commissionable gross profit reporting with cash-collected payout eligibility.',
    'category': 'Sales/Reporting',
    'author': 'Klouded',
    'license': 'LGPL-3',
    'depends': [
        'sale_management',
        'sale_margin',
        'account',
        'mail',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/commission_plan_data.xml',
        'views/commission_gp_views.xml',
        'views/commission_plan_views.xml',
        'views/product_template_views.xml',
    ],
    'installable': True,
    'application': True,
}
