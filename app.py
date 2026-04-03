from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, time, timedelta
from functools import wraps
import os, base64, io, csv, json
from PIL import Image

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pimo-etl-secret-dev-only')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///pimo_dtr.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7)
app.config['REMEMBER_COOKIE_SECURE'] = os.environ.get('DATABASE_URL') is not None
app.config['REMEMBER_COOKIE_HTTPONLY'] = True

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
SUPABASE_BUCKET = 'dtr-photos'


db = SQLAlchemy(app)
@app.context_processor
def inject_globals():
    if current_user.is_authenticated and current_user.is_admin:
        try:
            pw_pending = PasswordChangeRequest.query.filter_by(status='pending').count()
        except:
            pw_pending = 0
    else:
        pw_pending = 0
    return dict(pw_pending=pw_pending)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

UNITS = [
    'ADMINISTRATIVE', 'ADRIS', 'ASRIS', 'CASHIERING', 'CONSTRUCTION', 
    'DESIGN', 'ENGINEERING', 'EQUIPMENT', 'FINANCE', 'IDU', 
    'LARIS', 'OIM', 'PLANNING', 'PROPERTY', 'SURVEY', 'OTHER'
]

ADMIN_EMAIL = 'admin@etl.com'
ADMIN_PASSWORD = 'etladmin!@#'

# ─── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    middle_name = db.Column(db.String(80), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    unit = db.Column(db.String(50), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    dtr_logs = db.relationship('DTRLog', backref='user', lazy=True)

    @property
    def is_authenticated(self): return True
    @property
    def is_active(self): return True
    @property
    def is_anonymous(self): return False
    def get_id(self): return str(self.id)

    @property
    def full_name(self):
        mid = f' {self.middle_name[0]}.' if self.middle_name else ''
        return f'{self.first_name}{mid} {self.last_name}'

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class DTRLog(db.Model):
    __tablename__ = 'dtr_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    log_date = db.Column(db.Date, nullable=False)
    session_type = db.Column(db.String(10), nullable=False)  # 'AM' or 'PM'
    action = db.Column(db.String(10), nullable=False)        # 'IN' or 'OUT'
    timestamp = db.Column(db.DateTime, nullable=False)
    photo_path = db.Column(db.String(512), nullable=True)    # stores full Supabase public URL
    is_late = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    admin_remarks = db.Column(db.Text, nullable=True)

class PasswordChangeRequest(db.Model):
    __tablename__ = 'password_requests'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, approved, denied
    new_password_hash = db.Column(db.String(256), nullable=True)
    admin_note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User', backref='password_requests')


# ─── Login Manager ─────────────────────────────────────────────────────────────

@login_manager.user_loader
def load_user(user_id):
    if user_id == '0':
        admin = User()
        admin.id = 0
        admin.first_name = 'Admin'
        admin.last_name = 'ETL'
        admin.middle_name = ''
        admin.email = ADMIN_EMAIL
        admin.unit = 'ADMIN'
        admin.is_admin = True
        return admin
    return User.query.get(int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Admin access required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def intern_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.is_admin:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ─── Helpers ───────────────────────────────────────────────────────────────────

def save_photo(base64_data, user_id, log_type):
    try:
        import requests
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]
        img_bytes = base64.b64decode(base64_data)
        img = Image.open(io.BytesIO(img_bytes))
        img = img.convert('RGB')
        img.thumbnail((640, 480), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=75)
        buf.seek(0)

        fname = f"{user_id}_{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{fname}"
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/jpeg",
            "x-upsert": "true"
        }
        res = requests.put(url, data=buf.read(), headers=headers)

        if res.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{fname}"
        else:
            print(f"Upload failed: {res.status_code} {res.text}")
            return None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return None

def get_dtr_status(user_id, log_date):
    """Return dict with current DTR status for a user on a given date."""
    logs = DTRLog.query.filter_by(user_id=user_id, log_date=log_date).all()
    status = {
        'AM_IN': None, 'AM_OUT': None,
        'PM_IN': None, 'PM_OUT': None,
    }
    for log in logs:
        key = f"{log.session_type}_{log.action}"
        status[key] = log
    return status


def check_late(session_type, action):
    """Returns True if the current time is past the allowed limit."""
    now = datetime.now().time()
    if session_type == 'AM' and action == 'IN':
        return now > time(8, 0)
    if session_type == 'PM' and action == 'IN':
        return now > time(13, 0)
    return False


# Test mode stored in session
def is_monday():
    if session.get('test_mode'):
        return True
    return date.today().weekday() == 0


def can_log(session_type, action):
    """Check if logging is currently allowed."""
    now = datetime.now().time()
    if not is_monday():
        return False, "Attendance is only recorded on Mondays."
    if session_type == 'AM':
        if action == 'IN':
            return True, ""
        if action == 'OUT':
            if now >= time(12, 0):
                return True, ""
            return False, "AM Time Out is available from 12:00 PM."
    if session_type == 'PM':
        if action == 'IN':
            if now >= time(12, 0):
                return True, ""
            return False, "PM Time In is available from 12:00 PM."
        if action == 'OUT':
            if now >= time(17, 0):
                return True, ""
            return False, "PM Time Out is available from 5:00 PM."
    return False, "Invalid log type."


# ─── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard' if current_user.is_admin else 'intern_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            admin = load_user('0')
            login_user(admin, remember=remember)
            return redirect(url_for('admin_dashboard'))

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=remember)
            return redirect(url_for('intern_dashboard'))

        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        middle_name = request.form.get('middle_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        unit = request.form.get('unit', '')

        if not all([first_name, last_name, email, password, unit]):
            flash('Please fill in all required fields.', 'danger')
        elif password != confirm:
            flash('Passwords do not match.', 'danger')
        elif len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
        elif unit not in UNITS:
            flash('Please select a valid unit.', 'danger')
        elif User.query.filter_by(email=email).first():
            flash('Email is already registered.', 'danger')
        elif email == ADMIN_EMAIL:
            flash('This email is reserved.', 'danger')
        else:
            user = User(
                first_name=first_name, last_name=last_name,
                middle_name=middle_name, email=email, unit=unit
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html', units=UNITS)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ─── Intern Routes ─────────────────────────────────────────────────────────────

@app.route('/intern/dashboard')
@login_required
@intern_required
def intern_dashboard():
    today = date.today()
    status = get_dtr_status(current_user.id, today)
    monday = is_monday()
    now = datetime.now()
    return render_template('intern/dashboard.html',
                           status=status, today=today,
                           monday=monday, now=now)


@app.route('/intern/log', methods=['POST'])
@login_required
@intern_required
def intern_log():
    data = request.get_json()
    session_type = data.get('session_type')
    action = data.get('action')
    photo_b64 = data.get('photo')

    if session_type not in ('AM', 'PM') or action not in ('IN', 'OUT'):
        return jsonify({'success': False, 'message': 'Invalid log parameters.'})

    ok, msg = can_log(session_type, action)
    if not ok:
        return jsonify({'success': False, 'message': msg})

    today = date.today()
    existing = DTRLog.query.filter_by(
        user_id=current_user.id, log_date=today,
        session_type=session_type, action=action
    ).first()
    if existing:
        return jsonify({'success': False, 'message': 'Already logged.'})

    if not photo_b64:
        return jsonify({'success': False, 'message': 'Photo is required.'})

    photo_path = save_photo(photo_b64, current_user.id, f"{session_type}_{action}")
    is_late = check_late(session_type, action)

    log = DTRLog(
        user_id=current_user.id, log_date=today,
        session_type=session_type, action=action,
        timestamp=datetime.now(), photo_path=photo_path,
        is_late=is_late
    )
    db.session.add(log)
    db.session.commit()

    msg = f"{'⚠️ LATE – ' if is_late else ''}Time {action.title()} recorded for {session_type} session."
    return jsonify({'success': True, 'message': msg, 'is_late': is_late})


@app.route('/intern/history')
@login_required
@intern_required
def intern_history():
    logs = DTRLog.query.filter_by(user_id=current_user.id)\
                       .order_by(DTRLog.log_date.desc(), DTRLog.timestamp.desc()).all()
    return render_template('intern/history.html', logs=logs)

@app.route('/intern/profile', methods=['GET', 'POST'])
@login_required
@intern_required
def intern_profile():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_info':
            first_name = request.form.get('first_name', '').strip()
            last_name = request.form.get('last_name', '').strip()
            middle_name = request.form.get('middle_name', '').strip()
            unit = request.form.get('unit', '')
            if not all([first_name, last_name, unit]):
                flash('Please fill in all required fields.', 'danger')
            elif unit not in UNITS:
                flash('Invalid unit.', 'danger')
            else:
                current_user.first_name = first_name
                current_user.last_name = last_name
                current_user.middle_name = middle_name
                current_user.unit = unit
                db.session.commit()
                flash('Profile updated successfully.', 'success')

        elif action == 'request_password':
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            reason = request.form.get('reason', '').strip()
            if len(new_password) < 6:
                flash('Password must be at least 6 characters.', 'danger')
            elif new_password != confirm_password:
                flash('Passwords do not match.', 'danger')
            else:
                # Check no pending request already
                existing = PasswordChangeRequest.query.filter_by(
                    user_id=current_user.id, status='pending'
                ).first()
                if existing:
                    flash('You already have a pending password change request.', 'danger')
                else:
                    req = PasswordChangeRequest(
                        user_id=current_user.id,
                        reason=reason,
                        new_password_hash=generate_password_hash(new_password)
                    )
                    db.session.add(req)
                    db.session.commit()
                    flash('Password change request submitted. Waiting for admin approval.', 'success')

        return redirect(url_for('intern_profile'))

    pending_request = PasswordChangeRequest.query.filter_by(
        user_id=current_user.id, status='pending'
    ).first()
    past_requests = PasswordChangeRequest.query.filter_by(
        user_id=current_user.id
    ).order_by(PasswordChangeRequest.created_at.desc()).limit(5).all()

    return render_template('intern/profile.html',
                           units=UNITS,
                           pending_request=pending_request,
                           past_requests=past_requests)

# ─── Admin Routes ──────────────────────────────────────────────────────────────

@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    total_interns = User.query.filter_by(is_admin=False).count()
    today = date.today()
    today_logs = DTRLog.query.filter_by(log_date=today).count()
    unverified = DTRLog.query.filter_by(log_date=today, is_verified=False).count()
    recent_logs = DTRLog.query.order_by(DTRLog.timestamp.desc()).limit(10).all()
    pending_pw_requests = PasswordChangeRequest.query.filter_by(status='pending').count()
    return render_template('admin/dashboard.html',
                           total_interns=total_interns,
                           today_logs=today_logs,
                           unverified=unverified,
                           recent_logs=recent_logs,
                           today=today,
                           pending_pw_requests=pending_pw_requests)


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users = User.query.filter_by(is_admin=False).order_by(User.last_name).all()
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/delete/<int:uid>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(uid):
    user = User.query.get_or_404(uid)
    DTRLog.query.filter_by(user_id=uid).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.full_name} deleted.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/attendance')
@login_required
@admin_required
def admin_attendance():
    selected_date_str = request.args.get('date', date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    users = User.query.filter_by(is_admin=False).order_by(User.last_name).all()
    attendance = {}
    for user in users:
        attendance[user.id] = get_dtr_status(user.id, selected_date)

    return render_template('admin/attendance.html',
                           users=users, attendance=attendance,
                           selected_date=selected_date)

@app.route('/admin/password-requests')
@login_required
@admin_required
def admin_password_requests():
    pending = PasswordChangeRequest.query.filter_by(status='pending')\
                                         .order_by(PasswordChangeRequest.created_at.asc()).all()
    resolved = PasswordChangeRequest.query.filter(PasswordChangeRequest.status != 'pending')\
                                          .order_by(PasswordChangeRequest.resolved_at.desc()).limit(20).all()
    return render_template('admin/password_requests.html',
                           pending=pending, resolved=resolved)


@app.route('/admin/password-requests/<int:req_id>/approve', methods=['POST'])
@login_required
@admin_required
def admin_approve_password(req_id):
    req = PasswordChangeRequest.query.get_or_404(req_id)
    admin_note = request.form.get('admin_note', '').strip()
    user = User.query.get(req.user_id)
    user.password_hash = req.new_password_hash
    req.status = 'approved'
    req.admin_note = admin_note
    req.resolved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/password-requests/<int:req_id>/deny', methods=['POST'])
@login_required
@admin_required
def admin_deny_password(req_id):
    req = PasswordChangeRequest.query.get_or_404(req_id)
    admin_note = request.form.get('admin_note', '').strip()
    req.status = 'denied'
    req.admin_note = admin_note
    req.resolved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/admin/verify/<int:log_id>', methods=['POST'])
@login_required
@admin_required
def admin_verify(log_id):
    log = DTRLog.query.get_or_404(log_id)
    log.is_verified = True
    remarks = request.form.get('remarks', '').strip()
    if remarks:
        log.admin_remarks = remarks
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/remarks/<int:log_id>', methods=['POST'])
@login_required
@admin_required
def admin_remarks(log_id):
    log = DTRLog.query.get_or_404(log_id)
    log.admin_remarks = request.form.get('remarks', '').strip()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/export/csv')
@login_required
@admin_required
def export_csv():
    selected_date_str = request.args.get('date', date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    users = User.query.filter_by(is_admin=False).order_by(User.last_name).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Full Name', 'Unit', 'AM In', 'AM In Late', 'AM Out',
                     'PM In', 'PM In Late', 'PM Out', 'Verified', 'Remarks'])

    for user in users:
        s = get_dtr_status(user.id, selected_date)
        def fmt(log): return log.timestamp.strftime('%H:%M:%S') if log else '--'
        def late(log): return 'LATE' if log and log.is_late else ''
        verified = 'Yes' if any(l and l.is_verified for l in s.values() if l) else 'No'
        remarks = next((l.admin_remarks for l in s.values() if l and l.admin_remarks), '')
        writer.writerow([
            selected_date.isoformat(), user.full_name, user.unit,
            fmt(s['AM_IN']), late(s['AM_IN']), fmt(s['AM_OUT']),
            fmt(s['PM_IN']), late(s['PM_IN']), fmt(s['PM_OUT']),
            verified, remarks
        ])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'DTR_{selected_date_str}.csv'
    )


@app.route('/admin/export/pdf')
@login_required
@admin_required
def export_pdf():
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import cm

    selected_date_str = request.args.get('date', date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                            leftMargin=1*cm, rightMargin=1*cm,
                            topMargin=1.5*cm, bottomMargin=1*cm)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph('PIMO-ETL WFH Daily Time Record', styles['Title']))
    elements.append(Paragraph(f'Date: {selected_date.strftime("%B %d, %Y")} | Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}', styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))

    headers = ['Name', 'Unit', 'AM In', 'Late', 'AM Out', 'PM In', 'Late', 'PM Out', 'Verified', 'Remarks']
    data = [headers]
    users = User.query.filter_by(is_admin=False).order_by(User.last_name).all()

    for user in users:
        s = get_dtr_status(user.id, selected_date)
        def fmt(log): return log.timestamp.strftime('%H:%M') if log else '--'
        def late(log): return '⚠' if log and log.is_late else ''
        verified = '✓' if any(l and l.is_verified for l in s.values() if l) else ''
        remarks = next((l.admin_remarks for l in s.values() if l and l.admin_remarks), '')
        data.append([
            user.full_name, user.unit,
            fmt(s['AM_IN']), late(s['AM_IN']), fmt(s['AM_OUT']),
            fmt(s['PM_IN']), late(s['PM_IN']), fmt(s['PM_OUT']),
            verified, remarks[:30]
        ])

    col_widths = [4.5*cm, 2.5*cm, 2*cm, 1.2*cm, 2*cm, 2*cm, 1.2*cm, 2*cm, 2*cm, 4.5*cm]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0d3b6e')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f0f4f8')]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#d0d7e0')),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True,
                     download_name=f'DTR_{selected_date_str}.pdf')


# ─── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/status')
@login_required
def api_status():
    if current_user.is_admin:
        return jsonify({'role': 'admin'})
    today = date.today()
    status = get_dtr_status(current_user.id, today)
    result = {}
    for key, log in status.items():
        result[key] = {
            'logged': log is not None,
            'time': log.timestamp.strftime('%H:%M:%S') if log else None,
            'is_late': log.is_late if log else False,
            'is_verified': log.is_verified if log else False,
        }
    return jsonify({
        'role': 'intern',
        'is_monday': is_monday(),
        'status': result,
        'now': datetime.now().strftime('%H:%M:%S')
    })

#for test mode
@app.route('/intern/toggle-test-mode', methods=['POST'])
@login_required
def intern_toggle_test_mode():
    session['test_mode'] = not session.get('test_mode', False)
    return redirect(request.referrer or url_for('admin_dashboard'))

@app.route('/admin/attendance/delete/<int:log_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_log(log_id):
    log = DTRLog.query.get_or_404(log_id)
    db.session.delete(log)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/intern/export/pdf')
@login_required
@intern_required
def intern_export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import cm

    selected_date_str = request.args.get('date', '')
    try:
        selected_date = date.fromisoformat(selected_date_str) if selected_date_str else None
    except ValueError:
        selected_date = None

    query = DTRLog.query.filter_by(user_id=current_user.id)
    if selected_date:
        query = query.filter_by(log_date=selected_date)
    logs = query.order_by(DTRLog.log_date.desc(), DTRLog.timestamp.asc()).all()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    elements = []

    # Header
    title_style = ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#0d3b6e'), spaceAfter=4)
    sub_style = ParagraphStyle('sub', fontName='Helvetica', fontSize=10, textColor=colors.HexColor('#6b7b9a'), spaceAfter=2)

    elements.append(Paragraph('PIMO-ETL WFH Daily Time Record', title_style))
    elements.append(Paragraph(f'Intern: {current_user.full_name}   |   Unit: {current_user.unit}   |   Email: {current_user.email}', sub_style))
    if selected_date:
        elements.append(Paragraph(f'Date: {selected_date.strftime("%B %d, %Y")}', sub_style))
    else:
        elements.append(Paragraph('All Attendance Records', sub_style))
    elements.append(Paragraph(f'Generated: {datetime.now().strftime("%B %d, %Y %I:%M %p")}', sub_style))
    elements.append(Spacer(1, 0.5*cm))

    if logs:
        # Group by date
        from itertools import groupby
        grouped = groupby(logs, key=lambda l: l.log_date)
        for log_date, date_logs in grouped:
            date_logs = list(date_logs)
            # Date header row
            elements.append(Paragraph(
                f'{log_date.strftime("%A, %B %d, %Y")}',
                ParagraphStyle('dh', fontName='Helvetica-Bold', fontSize=11,
                               textColor=colors.white,
                               backColor=colors.HexColor('#0d3b6e'),
                               spaceBefore=10, spaceAfter=2,
                               leftIndent=6, rightIndent=6, leading=16)
            ))

            data = [['Session', 'Action', 'Time', 'Punctuality', 'Verified', 'Remarks']]
            for log in date_logs:
                data.append([
                    log.session_type,
                    log.action,
                    log.timestamp.strftime('%I:%M:%S %p'),
                    'LATE' if log.is_late else 'On Time',
                    'Verified' if log.is_verified else 'Pending',
                    log.admin_remarks or '—'
                ])

            col_widths = [2*cm, 2*cm, 3.5*cm, 2.5*cm, 2.5*cm, 4.5*cm]
            t = Table(data, colWidths=col_widths)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a4f8a')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 9),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f0f4f8')]),
                ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#d0d7e0')),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('PADDING', (0,0), (-1,-1), 5),
                # Color late red
                *[('TEXTCOLOR', (3, i+1), (3, i+1), colors.HexColor('#dc2626'))
                  for i, log in enumerate(date_logs) if log.is_late],
                # Color verified green
                *[('TEXTCOLOR', (4, i+1), (4, i+1), colors.HexColor('#059669'))
                  for i, log in enumerate(date_logs) if log.is_verified],
            ]))
            elements.append(t)
            elements.append(Spacer(1, 0.3*cm))
    else:
        elements.append(Paragraph('No attendance records found.', styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    fname = f"DTR_{current_user.last_name}_{selected_date_str or 'all'}.pdf"
    return send_file(buffer, mimetype='application/pdf',
                     as_attachment=True, download_name=fname)

# ─── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
