{
    "name" : "RMA - Return Merchandise Authorization/ Management",
    "version" : "19.0.1.4",
    "category" : "Inventory",
    "license": "LGPL-3",
    "depends" : ['base', 'mail', 'sale', 'sale_management', 'stock', 'account', 'product','mrp', 'unico_stripe_terminal'],
    "author": "Klouded Inc",
    'summary': 'RMA Return Product Replace product Refund Order Return order Merchandise Authorization website RMA product RMA management system product Return Merchandise website return order webshop RMA replace order customer RMA customer Return order Merchandise item',
    "description": """
        Odoo Return Order management
    """,

    "data": [
        'security/ir.model.access.csv',
        'security/rma_security.xml',
        'data/return_fee_product.xml',
        'data/legacy_rma_location.xml',
        'views/rma_order_sequence.xml',
        'views/rma_view.xml',
        'views/legacy_entitlement_views.xml',
        'views/sale_order_view.xml',
        'wizard/return_order.xml',
    ],
    "application": True,
    "auto_install": False,
    "installable": True,
}
