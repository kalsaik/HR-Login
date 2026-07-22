import base64
import json
from datetime import datetime, date, timedelta

import pytz
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


def _fmt_mins(mins):
    """Format integer minutes into '45m' or '1h 30m' etc."""
    mins = int(mins)
    if mins < 60:
        return f'{mins}m'
    h, m = divmod(mins, 60)
    return f'{h}h {m}m' if m else f'{h}h'


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

    # ── /my/attendance ──────────────────────────────────────────────────────

    @http.route('/my/attendance', type='http', auth='user', website=True)
    def portal_my_attendance(self, success=None, **kwargs):
        env = request.env
        employee = env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')
        employee = env['hr.employee'].sudo().browse(employee.id)

        # Soft dependency: hr.attendance may not be installed on every instance
        try:
            env['hr.attendance'].sudo()
            has_attendance = True
        except KeyError:
            has_attendance = False

        allow_adjustment = (
            _get_param('hr_holidays_portal.allow_attendance_adjustment', '1') == '1'
        )
        days_display = []
        month_hours = 0.0
        month_days = 0
        month_label = ''
        adjustment_requests = []

        if has_attendance:
            user_tz = pytz.timezone(env.user.tz or 'UTC')
            now_local = datetime.now(user_tz)
            today_local = now_local.date()
            month_label = now_local.strftime('%B %Y')

            first_of_month_utc = now_local.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            ).astimezone(pytz.UTC).replace(tzinfo=None)

            month_recs = env['hr.attendance'].sudo().search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', first_of_month_utc),
            ])
            month_hours = sum(r.worked_hours or 0.0 for r in month_recs)
            month_days = len({
                r.check_in.replace(tzinfo=pytz.UTC).astimezone(user_tz).date()
                for r in month_recs if r.check_in
            })

            # Most-recent 90 raw attendance records
            raw = env['hr.attendance'].sudo().search([
                ('employee_id', '=', employee.id),
            ], order='check_in desc', limit=90)

            # Public holidays: one year back, one year forward
            today_dt = date.today()
            ph_records = env['resource.calendar.leaves'].sudo().search([
                ('calendar_id', '=', False),
                ('date_from', '>=', datetime(today_dt.year - 1, 1, 1)),
                ('date_from', '<=', datetime(today_dt.year + 1, 12, 31)),
            ])
            holiday_map = {
                ph.date_from.date(): (ph.name or 'Public Holiday')
                for ph in ph_records if ph.date_from
            }

            # Work schedule for Late / OT computation (calendar tz aware)
            cal = employee.resource_calendar_id
            cal_tz = user_tz
            if cal and cal.tz:
                try:
                    cal_tz = pytz.timezone(cal.tz)
                except Exception:
                    pass

            schedule = {}
            if cal and cal.attendance_ids:
                for ca in cal.attendance_ids:
                    dow = ca.dayofweek
                    if dow not in schedule:
                        schedule[dow] = {
                            'start': ca.hour_from,
                            'end': ca.hour_to,
                        }
                    else:
                        schedule[dow]['start'] = min(
                            schedule[dow]['start'], ca.hour_from
                        )
                        schedule[dow]['end'] = max(
                            schedule[dow]['end'], ca.hour_to
                        )

            # Group raw records by local date
            day_sessions_map = {}
            for rec in raw:
                if not rec.check_in:
                    continue
                ci_local = rec.check_in.replace(tzinfo=pytz.UTC).astimezone(user_tz)
                day_sessions_map.setdefault(ci_local.date(), []).append(rec)

            for day_date in sorted(day_sessions_map.keys(), reverse=True):
                sessions_recs = sorted(
                    day_sessions_map[day_date], key=lambda r: r.check_in
                )
                is_today = (day_date == today_local)
                sessions = []
                total_hours = 0.0

                for rec in sessions_recs[:2]:
                    ci_local = rec.check_in.replace(
                        tzinfo=pytz.UTC
                    ).astimezone(user_tz)
                    co_str = None
                    if rec.check_out:
                        co_local = rec.check_out.replace(
                            tzinfo=pytz.UTC
                        ).astimezone(user_tz)
                        co_str = co_local.strftime('%H:%M')
                    sessions.append({
                        'check_in_str': ci_local.strftime('%H:%M'),
                        'check_out_str': co_str,
                        'worked_hours': rec.worked_hours or 0.0,
                        'is_open': not rec.check_out,
                    })
                    total_hours += rec.worked_hours or 0.0

                missing_punch = (not is_today) and any(
                    s['is_open'] for s in sessions
                )

                dow_str = str(day_date.weekday())
                is_late = False
                late_str = ''
                has_ot = False
                ot_str = ''

                if schedule.get(dow_str) and sessions_recs:
                    sched = schedule[dow_str]
                    first_ci_cal = sessions_recs[0].check_in.replace(
                        tzinfo=pytz.UTC
                    ).astimezone(cal_tz)
                    ci_hours = first_ci_cal.hour + first_ci_cal.minute / 60.0
                    late_mins = round((ci_hours - sched['start']) * 60)
                    if late_mins > 0:
                        is_late = True
                        late_str = _fmt_mins(late_mins)

                    last_rec = sessions_recs[-1]
                    if last_rec.check_out:
                        last_co_cal = last_rec.check_out.replace(
                            tzinfo=pytz.UTC
                        ).astimezone(cal_tz)
                        co_hours = last_co_cal.hour + last_co_cal.minute / 60.0
                        ot_mins = round((co_hours - sched['end']) * 60)
                        if ot_mins > 0:
                            has_ot = True
                            ot_str = _fmt_mins(ot_mins)

                days_display.append({
                    'date_str':    day_date.strftime('%d %b %Y'),
                    'weekday_str': day_date.strftime('%a'),
                    'month_key':   day_date.strftime('%B %Y'),
                    'date_iso':    day_date.isoformat(),
                    'sessions':    sessions,
                    'total_hours': total_hours,
                    'is_holiday':  day_date in holiday_map,
                    'holiday_name': holiday_map.get(day_date, ''),
                    'is_late':     is_late,
                    'late_str':    late_str,
                    'has_ot':      has_ot,
                    'ot_str':      ot_str,
                    'missing_punch': missing_punch,
                    'is_today':    is_today,
                })

            if allow_adjustment:
                adj_recs = env['hr.attendance.adjustment'].sudo().search([
                    ('employee_id', '=', employee.id),
                ], order='date desc', limit=10)
                for adj in adj_recs:
                    ci_str = ''
                    co_str = ''
                    if adj.check_in_requested:
                        ci_local = adj.check_in_requested.replace(
                            tzinfo=pytz.UTC
                        ).astimezone(user_tz)
                        ci_str = ci_local.strftime('%H:%M')
                    if adj.check_out_requested:
                        co_local = adj.check_out_requested.replace(
                            tzinfo=pytz.UTC
                        ).astimezone(user_tz)
                        co_str = co_local.strftime('%H:%M')
                    adjustment_requests.append({
                        'date_str':      adj.date.strftime('%d %b %Y') if adj.date else '',
                        'check_in_str':  ci_str,
                        'check_out_str': co_str,
                        'reason':        adj.reason or '',
                        'state':         adj.state,
                    })

        values = self._prepare_portal_layout_values()
        values.update({
            'employee':            employee,
            'days_display':        days_display,
            'has_attendance':      has_attendance,
            'month_hours':         month_hours,
            'month_days':          month_days,
            'month_label':         month_label,
            'allow_adjustment':    allow_adjustment,
            'adjustment_requests': adjustment_requests,
            'adj_success':         bool(success),
            'page_name':           'attendance',
        })
        return request.render('hr_holidays_portal.portal_my_attendance', values)

    # ── /my/attendance/adjust/new  GET ──────────────────────────────────────

    @http.route('/my/attendance/adjust/new', type='http', auth='user',
                website=True, methods=['GET'])
    def portal_attend_adjust_new(self, date=None, error=None, **kwargs):
        env = request.env
        allow_adjustment = (
            _get_param('hr_holidays_portal.allow_attendance_adjustment', '1') == '1'
        )
        if not allow_adjustment:
            return request.redirect('/my/attendance')

        employee = env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')
        employee = env['hr.employee'].sudo().browse(employee.id)

        values = self._prepare_portal_layout_values()
        values.update({
            'employee':  employee,
            'page_name': 'attend_adjust',
            'form_date': date or '',
            'error':     error,
        })
        return request.render('hr_holidays_portal.portal_attend_adjust_new', values)

    # ── /my/attendance/adjust/new  POST ─────────────────────────────────────

    @http.route('/my/attendance/adjust/new', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_attend_adjust_submit(self, **post):
        from urllib.parse import urlencode

        env = request.env
        allow_adjustment = (
            _get_param('hr_holidays_portal.allow_attendance_adjustment', '1') == '1'
        )
        if not allow_adjustment:
            return request.redirect('/my/attendance')

        employee = env.user.employee_id
        if not employee:
            return request.redirect('/my/allocations')
        employee = env['hr.employee'].sudo().browse(employee.id)

        date_str     = (post.get('date')     or '').strip()
        time_in_str  = (post.get('time_in')  or '').strip()
        time_out_str = (post.get('time_out') or '').strip()
        reason       = (post.get('reason')   or '').strip()

        def redirect_error(msg):
            params = {'date': date_str, 'error': msg}
            return request.redirect(
                '/my/attendance/adjust/new?' + urlencode(params)
            )

        if not date_str:
            return redirect_error('Please select a date.')
        if not time_in_str and not time_out_str:
            return redirect_error(
                'Please provide at least one time (check-in or check-out).'
            )
        if not reason:
            return redirect_error('Please provide a reason for the adjustment.')

        try:
            adj_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return redirect_error('Invalid date format.')

        user_tz = pytz.timezone(env.user.tz or 'UTC')
        check_in_utc = None
        check_out_utc = None

        if time_in_str:
            try:
                naive_ci = datetime.strptime(
                    f'{date_str} {time_in_str}', '%Y-%m-%d %H:%M'
                )
                check_in_utc = user_tz.localize(naive_ci).astimezone(
                    pytz.UTC
                ).replace(tzinfo=None)
            except ValueError:
                return redirect_error('Invalid check-in time format.')

        if time_out_str:
            try:
                naive_co = datetime.strptime(
                    f'{date_str} {time_out_str}', '%Y-%m-%d %H:%M'
                )
                check_out_utc = user_tz.localize(naive_co).astimezone(
                    pytz.UTC
                ).replace(tzinfo=None)
            except ValueError:
                return redirect_error('Invalid check-out time format.')

        if check_in_utc and check_out_utc and check_out_utc <= check_in_utc:
            return redirect_error('Check-out time must be after check-in time.')

        try:
            adj = env['hr.attendance.adjustment'].sudo().create({
                'employee_id':        employee.id,
                'date':               adj_date,
                'check_in_requested':  check_in_utc,
                'check_out_requested': check_out_utc,
                'reason':             reason,
                'state':              'submitted',
            })
        except Exception:
            return redirect_error(
                'Could not submit your request. Please try again.'
            )

        # Notify HR managers
        try:
            hr_mgr_group = env.ref('hr_holidays.group_hr_holidays_manager')
            hr_managers = env['res.users'].sudo().search([
                ('groups_id', 'in', [hr_mgr_group.id]),
                ('active', '=', True),
            ], limit=5)
            partner_ids = [u.partner_id.id for u in hr_managers if u.partner_id]
            if partner_ids:
                in_line = (
                    f'<li><strong>Check-in:</strong> {time_in_str}</li>'
                    if time_in_str else ''
                )
                out_line = (
                    f'<li><strong>Check-out:</strong> {time_out_str}</li>'
                    if time_out_str else ''
                )
                adj.message_post(
                    body=(
                        f'<p><strong>Attendance adjustment request</strong> '
                        f'from <strong>{employee.name}</strong></p>'
                        f'<ul>'
                        f'<li><strong>Date:</strong> '
                        f'{adj_date.strftime("%d %b %Y")}</li>'
                        + in_line + out_line
                        + f'<li><strong>Reason:</strong> {reason}</li>'
                        + f'</ul>'
                    ),
                    partner_ids=partner_ids,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
        except Exception:
            pass

        return request.redirect('/my/attendance?success=1')

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

        values = self._prepare_portal_layout_values()
        values.update({
            'leave': leave,
            'employee': employee,
            'attachments': attachments,
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
