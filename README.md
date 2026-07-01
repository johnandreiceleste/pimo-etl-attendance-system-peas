# PIMO-ETL WFH DTR System

A Flask-based Daily Time Record web application for PIMO-ETL WFH interns.

## Features
- 🔐 Admin login (hardcoded) + Intern self-registration
- 📸 Live camera capture (no file upload allowed)
- 📅 Monday-only attendance logic with AM/PM sessions
- ⚠️ Late detection (AM In > 8:00, PM In > 13:00)
- ✅ Admin verification + remarks per intern
- 📤 Export to CSV and PDF
- 📱 PWA-ready, mobile responsive with sidebar

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the app
```bash
python app.py
```
The app will be available at: http://localhost:5000

### 3. Admin Login
- **Email:** admin@etl.com
- **Password:** etladmin!@#

### 4. Intern Registration
Interns visit `/register` and create an account with their unit.

## Project Structure
```
pimo-etl-dtr/
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── static/
│   ├── manifest.json       # PWA manifest
│   ├── icons/              # PWA icons (add your own)
│   └── uploads/            # Captured photos (auto-created)
└── templates/
    ├── base.html           # Base layout with sidebar
    ├── login.html          # Login page
    ├── register.html       # Intern registration
    ├── intern/
    │   ├── dashboard.html  # DTR capture page
    │   └── history.html    # Attendance history
    └── admin/
        ├── dashboard.html  # Admin overview
        ├── attendance.html # Date-based verification
        └── users.html      # Intern management
```

## Attendance Rules
| Session | Action | Available | Late if after |
|---------|--------|-----------|---------------|
| AM | Time In | Any time | 8:00 AM |
| AM | Time Out | 12:00 PM onward | — |
| PM | Time In | 12:00 PM onward | 1:00 PM |
| PM | Time Out | 5:00 PM onward | — |

> ⚠️ Attendance only records on **Mondays**.

