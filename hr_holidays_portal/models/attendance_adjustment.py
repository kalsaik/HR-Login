from odoo import models, fields


class HrAttendanceAdjustment(models.Model):
    _name = 'hr.attendance.adjustment'
    _description = 'Attendance Adjustment Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        tracking=True, ondelete='cascade',
    )
    date = fields.Date(string='Date', required=True, tracking=True)
    check_in_requested = fields.Datetime(
        string='Requested Check-in', tracking=True)
    check_out_requested = fields.Datetime(
        string='Requested Check-out', tracking=True)
    reason = fields.Text(string='Reason', required=True)
    state = fields.Selection([
        ('submitted', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='Status', default='submitted', tracking=True, required=True)
    # Integer ref avoids a hard Many2one FK on hr.attendance (soft dependency)
    attendance_ref_id = fields.Integer(
        string='Attendance Record ID', default=0,
        help='ID of the hr.attendance record being corrected. 0 = create new on approval.',
    )
    reviewer_id = fields.Many2one(
        'res.users', string='Reviewed by', readonly=True, tracking=True)
    review_note = fields.Text(string='Review Note')

    def action_approve(self):
        for rec in self:
            # Apply the adjustment to the attendance record (if hr_attendance is installed)
            try:
                if rec.attendance_ref_id:
                    att = self.env['hr.attendance'].browse(rec.attendance_ref_id)
                    if att.exists():
                        vals = {}
                        if rec.check_in_requested:
                            vals['check_in'] = rec.check_in_requested
                        if rec.check_out_requested:
                            vals['check_out'] = rec.check_out_requested
                        if vals:
                            att.sudo().write(vals)
                elif rec.check_in_requested:
                    # No existing record — create a new attendance entry
                    create_vals = {
                        'employee_id': rec.employee_id.id,
                        'check_in': rec.check_in_requested,
                    }
                    if rec.check_out_requested:
                        create_vals['check_out'] = rec.check_out_requested
                    self.env['hr.attendance'].sudo().create(create_vals)
            except (KeyError, Exception):
                pass  # hr.attendance not installed or record error

            rec.write({'state': 'approved', 'reviewer_id': self.env.user.id})

            # Notify the employee
            emp = rec.employee_id
            if emp.user_id and emp.user_id.partner_id:
                try:
                    rec.message_post(
                        body=(
                            f'Your attendance adjustment request for '
                            f'<strong>{rec.date}</strong> has been '
                            f'<strong style="color:#065F46">approved</strong>.'
                        ),
                        partner_ids=[emp.user_id.partner_id.id],
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                    )
                except Exception:
                    pass

    def action_reject(self):
        for rec in self:
            rec.write({'state': 'rejected', 'reviewer_id': self.env.user.id})
            emp = rec.employee_id
            if emp.user_id and emp.user_id.partner_id:
                try:
                    note_line = (
                        f'<br/>Note: {rec.review_note}' if rec.review_note else ''
                    )
                    rec.message_post(
                        body=(
                            f'Your attendance adjustment request for '
                            f'<strong>{rec.date}</strong> has been '
                            f'<strong style="color:#991B1B">rejected</strong>.{note_line}'
                        ),
                        partner_ids=[emp.user_id.partner_id.id],
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                    )
                except Exception:
                    pass
