{
    'name': 'Unico ACH Clearance Status',
    'version': '19.0.1.0.0',
    'summary': 'One-click list of sale orders whose ACH payment has not cleared Stripe yet',
    'description': """
ACH Direct Debit payments take several days to clear, unlike cards. Until the
Stripe transaction reaches Confirmed, the money is not really in. This adds a
Sales menu entry listing every order with an ACH payment still in flight, with
the amount, how long it has been pending, and the delivery status of the order,
so the counter can answer "has my payment cleared / has my order been released"
without opening Stripe.
""",
    'category': 'Sales/Sales',
    'author': 'Klouded',
    'license': 'LGPL-3',
    'depends': [
        'sale',
        'sale_stock',
        'payment',
    ],
    'data': [
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
