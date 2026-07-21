import base64
from datetime import datetime

from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal

ALLOWED_MIME = {'application/pdf', 'image/jpeg', 'image/png', 'image/jpg'}


def _get_param(key, default):
    """Read an ir.config_parameter value, falling back to *default*."""
    val = request.env['ir.config_parameter'].sudo().get_param(key)
    return val if val is not None else default


class HrHolidaysPortal(CustomerPortal):

    # ── Portal home counter ──────────────────────────────────────────────────

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'allocation_count' in counters:
            employee = request.env.user.employee_id
            if employee:
                allocation_count = request.env['hr.leave.allocation'].sudo().search_count([
                    ('employee_id', '=', employee.id),
                    ('state', '=', 'validate'),
                    ('holiday_status_id.active', '=', True),
                ])
            else:
                allocation_count = 0
            values['allocation_count'] = allocation_count
        return values

    # ── /my/allocations ──────────────────────────────────────────────────────

    @http.route('/my/allocations', type='http', auth='user', website=True)
    def portal_my_allocations(self, success=None, **kwargs):
        env = request.env
        employee = env.user.employee_id

        if employee:
            allocations = env['hr.leave.allocation'].sudo().search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'validate'),
                ('holiday_status_id.active', '=', True),
            ], order='holiday_status_id asc')

            pending_leaves = env['hr.leave'].sudo().search([
                ('employee_id', '=', employee.id),
                ('state', 'in', ['confirm', 'validate1']),
            ])
            pending_by_type = {}
            for leave in pending_leaves:
                tid = leave.holiday_status_id.id
                pending_by_type[tid] = pending_by_type.get(tid, 0) + leave.number_of_days

            recent_leaves = env['hr.leave'].sudo().search([
                ('employee_id', '=', employee.id),
                ('state', '!=', 'draft'),
            ], order='date_from desc', limit=5)
        else:
            allocations = env['hr.leave.allocation'].sudo().browse()
            pending_by_type = {}
            recent_leaves = env['hr.leave'].sudo().browse()

        values = self._prepare_portal_layout_values()
        values.update({
            'allocations': allocations,
            'employee': employee,
            'page_name': 'allocations',
            'pending_by_type': pending_by_type,
            'recent_leaves': recent_leaves,
            'success': bool(success),
        })
        return request.render('hr_holidays_portal.portal_my_allocations', values)

    # ── /my/leaves/new  GET ──────────────────────────────────────────────────

    @http.route('/my/leaves/new', type='http', auth='user', website=True,
                methods=['GET'])
    def portal_leave_new(self, error=None, date_from=None, date_to=None,
                         leave_type_id=None, **kwargs):
        employee = request.env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')

        sick_leave_name = _get_param(
            'hr_holidays_portal.sick_leave_name', 'Sick Leave')
        cert_threshold = int(_get_param(
            'hr_holidays_portal.cert_day_threshold', '3'))

        allocations = request.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('holiday_status_id.active', '=', True),
        ])

        seen = set()
        leave_types_data = []
        for alloc in allocations:
            lt = alloc.holiday_status_id
            if lt.id in seen:
                continue
            seen.add(lt.id)
            remaining = alloc.number_of_days - alloc.leaves_taken
            leave_types_data.append({
                'id': lt.id,
                'name': lt.name,
                'remaining': remaining,
                'is_sick': lt.name == sick_leave_name,
            })

        # Find sick leave type id for JS logic
        sl = request.env['hr.leave.type'].sudo().search(
            [('name', '=', sick_leave_name)], limit=1)

        values = self._prepare_portal_layout_values()
        values.update({
            'employee': employee,
            'leave_types_data': leave_types_data,
            'sick_leave_id': sl.id if sl else 0,
            'cert_threshold': cert_threshold,
            'page_name': 'leave_new',
            'error': error,
            'form_date_from': date_from or '',
            'form_date_to': date_to or '',
            'form_leave_type_id': int(leave_type_id) if leave_type_id else 0,
        })
        return request.render('hr_holidays_portal.portal_leave_new', values)

    # ── /my/leaves/new  POST ─────────────────────────────────────────────────

    @http.route('/my/leaves/new', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_leave_new_submit(self, **post):
        from urllib.parse import urlencode

        employee = request.env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')

        # Read configurable parameters
        sick_leave_name = _get_param(
            'hr_holidays_portal.sick_leave_name', 'Sick Leave')
        cert_threshold = int(_get_param(
            'hr_holidays_portal.cert_day_threshold', '3'))
        max_upload_bytes = int(_get_param(
            'hr_holidays_portal.max_upload_bytes', str(5 * 1024 * 1024)))

        leave_type_id = int(post.get('leave_type_id') or 0)
        date_from_str = (post.get('date_from') or '').strip()
        date_to_str   = (post.get('date_to')   or '').strip()
        reason        = (post.get('reason')     or '').strip()

        def redirect_error(msg):
            params = {'error': msg, 'date_from': date_from_str,
                      'date_to': date_to_str, 'leave_type_id': leave_type_id}
            return request.redirect('/my/leaves/new?' + urlencode(params))

        # ── Basic validation ─────────────────────────────────────────────────
        if not leave_type_id or not date_from_str or not date_to_str:
            return redirect_error('Please fill in all required fields.')

        try:
            date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date()
            date_to   = datetime.strptime(date_to_str,   '%Y-%m-%d').date()
        except ValueError:
            return redirect_error('Invalid date format. Please use the date picker.')

        if date_to < date_from:
            return redirect_error('End date cannot be before the start date.')

        # ── Verify employee has an allocation for this type ──────────────────
        alloc = request.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', employee.id),
            ('holiday_status_id', '=', leave_type_id),
            ('state', '=', 'validate'),
        ], limit=1)
        if not alloc:
            return redirect_error(
                'You do not have an approved allocation for this leave type.')

        leave_type = alloc.holiday_status_id
        calendar_days = (date_to - date_from).days + 1

        # ── Medical certificate check ────────────────────────────────────────
        medical_file = request.httprequest.files.get('medical_cert')
        needs_cert = (leave_type.name == sick_leave_name
                      and calendar_days >= cert_threshold)

        if needs_cert:
            if not medical_file or not medical_file.filename:
                return redirect_error(
                    f'A medical certificate is required for {sick_leave_name} '
                    f'of {cert_threshold} or more consecutive days.')
            file_bytes = medical_file.read()
            if len(file_bytes) > max_upload_bytes:
                mb = max_upload_bytes // (1024 * 1024)
                return redirect_error(f'File too large. Maximum size is {mb} MB.')
            mime = medical_file.content_type or ''
            if mime not in ALLOWED_MIME:
                return redirect_error(
                    'Invalid file type. Please upload a PDF or image (JPG/PNG).')
        elif medical_file and medical_file.filename:
            # Optional file attached even though not required
            file_bytes = medical_file.read()
            mime = medical_file.content_type or 'application/octet-stream'
        else:
            file_bytes = None
            mime = None

        # ── Create leave request ─────────────────────────────────────────────
        try:
            leave_vals = {
                'employee_id': employee.id,
                'holiday_status_id': leave_type_id,
                'request_date_from': date_from,
                'request_date_to': date_to,
            }
            if reason:
                leave_vals['name'] = reason

            leave = request.env['hr.leave'].sudo().create(leave_vals)
            # Odoo 19: hr.leave is created directly in 'confirm' state

        except Exception as e:
            return redirect_error(f'Could not submit request: {e}')

        # ── Attach medical certificate to chatter ────────────────────────────
        if file_bytes:
            try:
                attachment = request.env['ir.attachment'].sudo().create({
                    'name': medical_file.filename,
                    'datas': base64.b64encode(file_bytes).decode(),
                    'res_model': 'hr.leave',
                    'res_id': leave.id,
                    'mimetype': mime,
                })
                leave.sudo().message_post(
                    body='<p><strong>Medical certificate attached</strong> by employee.</p>',
                    attachment_ids=[attachment.id],
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
            except Exception:
                pass  # Attachment failure should not block submission

        # ── Notify the leave manager ─────────────────────────────────────────
        manager = employee.leave_manager_id
        if manager and manager.partner_id:
            try:
                leave.sudo().message_post(
                    body=(
                        f'<p>📋 <strong>New leave request</strong> from '
                        f'<strong>{employee.name}</strong> is awaiting your approval.</p>'
                        f'<ul>'
                        f'<li><strong>Type:</strong> {leave_type.name}</li>'
                        f'<li><strong>From:</strong> {date_from.strftime("%d %b %Y")}</li>'
                        f'<li><strong>To:</strong> {date_to.strftime("%d %b %Y")}</li>'
                        f'<li><strong>Duration:</strong> {leave.number_of_days:.0f} working day(s)</li>'
                        + (f'<li><strong>Reason:</strong> {reason}</li>' if reason else '')
                        + '</ul>'
                    ),
                    partner_ids=[manager.partner_id.id],
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
                leave.sudo().activity_schedule(
                    'mail.mail_activity_data_todo',
                    summary=f'Approve leave — {employee.name} ({leave_type.name})',
                    user_id=manager.id,
                )
            except Exception:
                pass  # Notification failure should not block submission

        return request.redirect('/my/allocations?success=1')
