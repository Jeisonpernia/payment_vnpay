# -*- coding: utf-8 -*-

{
    'name': 'VNPAY Payment Acquirer',
    'category': 'Hidden',
    'summary': 'Payment Acquirer: VNPAY Implementation',
    'version': '1.0',
    'author': 'SCISoftware',
    'description': """VNPAY Payment Acquirer""",
    'depends': ['payment'],
    'data': [
        'views/payment_views.xml',
        'views/payment_vnpay_templates.xml',
        'data/payment_acquirer_data.xml',
    ],
    'images': ['static/description/icon.png'],
    'installable': True,
    'post_init_hook': 'create_missing_journal_for_acquirers',
}
