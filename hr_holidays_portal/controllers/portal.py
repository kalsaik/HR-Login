import base64
import json
from datetime import datetime, date, timedelta

from markupsafe import Markup

from odoo import http
from odoo.exceptions import ValidationError, UserError
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal

ALLOWED_MIME = {'application/pdf', 'image/jpeg', 'image/png', 'image/jpg'}


def _get_param(key, default):
    """Read an ir.config_parameter value, falling back to *default*."""
    val = request.env['ir.config_parameter'].sudo().get_param(key)
    return val if val is not None else default


def _safe_json(data):
    """Serialise *data* to JSON and mark it safe for QWeb t-out."""
    return Markup(json.dumps(data))


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
    def portal_my_allocations(self, success=None, cancelled=None, **kwargs):
        env = request.env
        employee = env.user.employee_id

        if employee:
            # Refresh employee via sudo so versioned fields (job_title, dept) are readable
            employee = env['hr.employee'].sudo().browse(employee.id)
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
            'cancelled': bool(cancelled),
        })
        return request.render('hr_holidays_portal.portal_my_allocations', values)

    # ── /my/leaves — full leave history ─────────────────────────────────────

    @http.route('/my/leaves', type='http', auth='user', website=True)
    def portal_my_leaves(self, **kwargs):
        env = request.env
        employee = env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')

        employee = env['hr.employee'].sudo().browse(employee.id)
        all_leaves = env['hr.leave'].sudo().search([
            ('employee_id', '=', employee.id),
            ('state', '!=', 'draft'),
        ], order='date_from desc')

        values = self._prepare_portal_layout_values()
        values.update({
            'employee': employee,
            'all_leaves': all_leaves,
            'page_name': 'leaves_all',
        })
        return request.render('hr_holidays_portal.portal_my_leaves', values)

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

        # Sick leave type id for JS cert logic
        sl = request.env['hr.leave.type'].sudo().search(
            [('name', '=', sick_leave_name)], limit=1)

        # Public holidays — past year and next year (allow past dates)
        today = date.today()
        ph_records = request.env['resource.calendar.leaves'].sudo().search([
            ('calendar_id', '=', False),
            ('date_from', '>=', today - timedelta(days=365)),
            ('date_from', '<=', today + timedelta(days=365)),
        ])
        holiday_dates = {
            h.date_from.date().strftime('%Y-%m-%d'): h.name or 'Public Holiday'
            for h in ph_records
        }

        # Colleagues in the same department for the covering-person dropdown.
        # department_id lives in hr_version in Odoo 19, so read it via sudo.
        emp_sudo = request.env['hr.employee'].sudo().browse(employee.id)
        dept_id = emp_sudo.department_id.id if emp_sudo.department_id else False
        if dept_id:
            colleagues = request.env['hr.employee'].sudo().search([
                ('department_id', '=', dept_id),
                ('id', '!=', employee.id),
                ('active', '=', True),
            ], order='name asc')
        else:
            colleagues = request.env['hr.employee'].sudo().browse()

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
            'colleagues': colleagues,
            # Markup-wrapped JSON is safe for t-out without HTML-escaping
            'holiday_dates_json': _safe_json(holiday_dates),
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

        leave_type_id    = int(post.get('leave_type_id') or 0)
        date_from_str    = (post.get('date_from') or '').strip()
        date_to_str      = (post.get('date_to')   or '').strip()
        reason           = (post.get('reason')     or '').strip()
        covering_id      = int(post.get('covering_person_id') or 0)

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

        # ── Verify allocation exists ─────────────────────────────────────────
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

        # ── Covering person (same-dept validation) ───────────────────────────
        covering = None
        if covering_id:
            cov = request.env['hr.employee'].sudo().browse(covering_id)
            if cov.exists() and cov.active:
                # Only accept if same department (or employee has no dept)
                if (not employee.department_id
                        or cov.department_id.id == employee.department_id.id):
                    covering = cov

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

        except (ValidationError, UserError) as e:
            msg = str(e)
            if 'overlap' in msg.lower() or 'already booked' in msg.lower():
                return redirect_error(
                    'You already have a leave request covering some or all of those dates. '
                    'Please check your Recent Requests before submitting.'
                )
            # Other business-rule errors — strip technical boilerplate, show cleanly
            clean = msg.split('\n')[0].strip()
            return redirect_error(clean or 'Your request could not be submitted. Please try again.')
        except Exception:
            return redirect_error('Something went wrong while submitting your request. Please try again.')

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
                pass

        # ── Notify the leave manager ─────────────────────────────────────────
        manager = employee.leave_manager_id
        if manager and manager.partner_id:
            try:
                covering_line = (
                    f'<li><strong>Covered by:</strong> {covering.name}</li>'
                    if covering else ''
                )
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
                        + covering_line
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
                pass

        return request.redirect('/my/allocations?success=1')

    # ── /my/leaves/<id>  GET ─────────────────────────────────────────────────

    @http.route('/my/leaves/<int:leave_id>', type='http', auth='user', website=True)
    def portal_leave_detail(self, leave_id, **kwargs):
        employee = request.env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')

        leave = request.env['hr.leave'].sudo().browse(leave_id)

        # Security: must exist and belong to this employee
        if not leave.exists() or leave.employee_id.id != employee.id:
            return request.redirect('/my/allocations')

        # Attachments on the leave record (medical certs etc.)
        attachments = request.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'hr.leave'),
            ('res_id', '=', leave.id),
        ])

        # Chatter messages — show comments only, skip empty/system ones
        messages = request.env['mail.message'].sudo().search([
            ('res_id', '=', leave.id),
            ('model', '=', 'hr.leave'),
            ('message_type', 'in', ['comment', 'email']),
            ('body', '!=', ''),
        ], order='date asc')

        values = self._prepare_portal_layout_values()
        values.update({
            'leave': leave,
            'employee': employee,
            'attachments': attachments,
            'messages': messages,
            'page_name': 'leave_detail',
            'can_cancel': leave.state in ('confirm', 'validate1'),
        })
        return request.render('hr_holidays_portal.portal_leave_detail', values)

    # ── /my/leaves/<id>/cancel  POST ─────────────────────────────────────────

    @http.route('/my/leaves/<int:leave_id>/cancel', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_leave_cancel(self, leave_id, **kwargs):
        employee = request.env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')

        leave = request.env['hr.leave'].sudo().browse(leave_id)

        # Security check
        if not leave.exists() or leave.employee_id.id != employee.id:
            return request.redirect('/my/allocations')

        if leave.state not in ('confirm', 'validate1'):
            return request.redirect(f'/my/leaves/{leave_id}')

        try:
            leave.sudo().action_refuse()
        except Exception:
            # Fallback: force state directly if action_refuse fails
            leave.sudo().write({'state': 'refuse'})

        try:
            leave.sudo().message_post(
                body='<p>❌ Leave request <strong>cancelled by employee</strong> via the self-service portal.</p>',
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception:
            pass

        return request.redirect('/my/allocations?cancelled=1')
