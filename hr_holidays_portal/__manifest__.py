{
    'name': 'HR Holidays Portal',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Allow portal users to view their leave allocations',
    'depends': ['hr_holidays', 'portal'],
    'data': [
        'data/config_parameters.xml',
        'views/portal_templates.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
