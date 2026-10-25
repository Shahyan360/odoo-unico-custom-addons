# -*- coding: utf-8 -*-
{
    'name': 'Unico Delivery Field Service',
    'version': '19.0.1.6.0',
    'summary': 'Create Field Service delivery execution tasks from local Sales Orders.',
    'category': 'Operations/Field Service',
    'author': 'Klouded',
    'license': 'LGPL-3',
    'depends': [
        'sale_management',
        'sale_stock',
        'sale_mrp',
        'account',
        'project',
        'fleet',
    ],
    'data': [
        'data/delivery_project_data.xml',
        'views/project_task_views.xml',
        'views/sale_order_views.xml',
        'reports/sale_report_templates.xml',
        'reports/account_invoice_report_templates.xml',
        'reports/stock_delivery_report_templates.xml',
    ],
    'installable': True,
    'application': False,
    'post_init_hook': '_post_init_hook',
}
