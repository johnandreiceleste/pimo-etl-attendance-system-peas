from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, time, timedelta
from functools import wraps
import os, base64, io, csv, json
from PIL import Image

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pimo-etl-secret-2024-secure-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pimo_dtr.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7)
app.config['REMEMBER_COOKIE_SECURE'] = False
app.config['REMEMBER_COOKIE_HTTPONLY'] = True

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

UNITS = [
    'PROPERTY', 'OIM', 'ADRIS', 'LARIS', 'ASRIS', 'ENGINEERING',
    'ADMINISTRATIVE', 'FINANCE', 'PLANNING', 'DESIGN', 'CONSTRUCTION',
    'EQUIPMENT', 'IDU', 'SURVEY', 'CASHIERING', 'OTHER'
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
    photo_path = db.Column(db.String(256), nullable=True)
    is_late = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    admin_remarks = db.Column(db.Text, nullable=True)


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
    """Decode base64 image, resize, and save to disk."""
    try:
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]
        img_bytes = base64.b64decode(base64_data)
        img = Image.open(io.BytesIO(img_bytes))
        img = img.convert('RGB')
        img.thumbnail((640, 480), Image.LANCZOS)
        fname = f"{user_id}_{log_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jpg"
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        img.save(fpath, 'JPEG', quality=75)
        return f"uploads/{fname}"
    except Exception as e:
        print(f"Photo save error: {e}")
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


def is_monday():
    return date.today().weekday() == 0  # 0 = Monday


def can_log(session_type, action):
    """Check if logging is currently allowed."""
    now = datetime.now().time()
    if not (is_monday() or session.get('test_mode', False)):
        return False, "Attendance is only recorded on Mondays (testing mode disabled)."
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

        # Admin check
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
    test_mode = session.get('test_mode', False)
    now = datetime.now()
    return render_template('intern/dashboard.html',
                           status=status, today=today,
                           monday=monday, test_mode=test_mode,
                           now=now)


@app.route('/intern/toggle-test-mode', methods=['POST'])
@login_required
@intern_required
def intern_toggle_test_mode():
    test_mode = session.get('test_mode', False)
    session['test_mode'] = not test_mode
    flash(f"Testing mode {'enabled' if not test_mode else 'disabled'}.", 'success')
    return redirect(url_for('intern_dashboard'))


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
    return render_template('admin/dashboard.html',
                           total_interns=total_interns,
                           today_logs=today_logs,
                           unverified=unverified,
                           recent_logs=recent_logs,
                           today=today)


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


# ─── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    os.makedirs(os.path.join('static', 'uploads'), exist_ok=True)
    app.run(debug=True, port=5000)
