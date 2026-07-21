{
    'name': 'HR Holidays Portal',
    'version': '19.0.1.0.0',
    'author': 'Coral Bay Maldives',
    'category': 'Human Resources',
    'summary': 'Employee self-service leave portal for portal users',
    'depends': ['hr_holidays', 'portal'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rules.xml',
        'data/config_parameters.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'hr_holidays_portal/static/src/css/portal.css',
        ],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
