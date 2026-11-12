import os
import gzip
import shutil
import sqlite3
import hashlib
import csv
import io
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, make_response
from apscheduler.schedulers.background import BackgroundScheduler

from license_guard import require_valid_license, verify_license_key, DEFAULT_DEV_KEY
from notifier import send_payment_receipt

app = Flask(__name__)

# Database and Environment Configuration
DATABASE_URL = os.environ.get("DATABASEING_URL") or os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("HOSPITAL_DB_PATH", "hospital_local_server.db")
BACKUP_DIR = os.environ.get("HOSPITAL_BACKUP_DIR", "./local_server_backups")
_staff_pin_env = os.environ.get("HOSPITAL_STAFF_PIN", "")
if not _staff_pin_env:
    import warnings as _warnings
    _warnings.warn(
        "HOSPITAL_STAFF_PIN is not set in environment. "
        "Set a strong PIN in your .env or Docker environment before going to production.",
        RuntimeWarning,
        stacklevel=2,
    )
    _staff_pin_env = "CHANGE_ME_NOW"
STAFF_PIN = _staff_pin_env

# Global Watchdog State for UI Banner Reminders
watchdog_state = {
    "alert_level": "NORMAL",  # NORMAL | REMINDER_1600 | URGENT_1730 | COMPLETED
    "message": "Daily backup schedule active (4:00 PM Reminder / 6:00 PM Auto-FailSafe)."
}

def get_db():
    """Returns a PostgreSQL connection if DATABASE_URL is set, otherwise falls back to SQLite."""
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def require_staff_pin(f):
    """Decorator to block unauthorized users from accessing admin/clinical write routes without the Staff PIN."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided_pin = request.headers.get("X-Staff-PIN") or (request.json or {}).get("staff_pin")
        if provided_pin != STAFF_PIN:
            return jsonify({"status": "UNAUTHORIZED", "error": "Invalid or missing Hospital Staff PIN."}), 403
        return f(*args, **kwargs)
    return decorated_function

def init_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = get_db()
    cur = conn.cursor()

    if DATABASE_URL:
        # PostgreSQL Schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_queue (
                id SERIAL PRIMARY KEY,
                identifier TEXT NOT NULL,
                full_name TEXT NOT NULL,
                checkin_method TEXT NOT NULL,
                department TEXT NOT NULL,
                priority TEXT DEFAULT 'Standard',
                ticket_number TEXT NOT NULL,
                status TEXT DEFAULT 'WAITING',
                payment_status TEXT DEFAULT 'PENDING',
                sheets_saved INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clinical_records (
                id SERIAL PRIMARY KEY,
                ticket_number TEXT NOT NULL,
                diagnosis TEXT,
                prescription TEXT,
                lab_notes TEXT,
                status TEXT DEFAULT 'PENDING_PHARMACY',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backup_audit_logs (
                id SERIAL PRIMARY KEY,
                backup_date TEXT NOT NULL,
                trigger_mode TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size_kb REAL NOT NULL,
                sha256_hash TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        # SQLite Schema
        conn.execute("PRAGMA journal_mode=WAL;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                full_name TEXT NOT NULL,
                checkin_method TEXT NOT NULL,
                department TEXT NOT NULL,
                priority TEXT DEFAULT 'Standard',
                ticket_number TEXT NOT NULL,
                status TEXT DEFAULT 'WAITING',
                payment_status TEXT DEFAULT 'PENDING',
                sheets_saved INTEGER DEFAULT 3,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clinical_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT NOT NULL,
                diagnosis TEXT,
                prescription TEXT,
                lab_notes TEXT,
                status TEXT DEFAULT 'PENDING_PHARMACY',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backup_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_date TEXT NOT NULL,
                trigger_mode TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size_kb REAL NOT NULL,
                sha256_hash TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
    conn.commit()
    cur.close()
    conn.close()

def is_backed_up_today():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM backup_audit_logs WHERE backup_date = %s LIMIT 1" if DATABASE_URL else "SELECT id FROM backup_audit_logs WHERE backup_date = ? LIMIT 1", (today,))
        row = cur.fetchone()
        cur.close()
        return row is not None

def run_hot_backup(trigger_mode="AUTO_FAILSAFE_1800"):
    """Uses SQLite Online Backup API + GZIP compression + SHA-256 Checksum (Applicable for SQLite local server modes)."""
    if DATABASE_URL:
        # Cloud Postgres doesn't use local file sqlite backups; handled by Render provider snapshots
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    if trigger_mode == "AUTO_FAILSAFE_1800" and is_backed_up_today():
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_temp_db = os.path.join(BACKUP_DIR, f"temp_snapshot_{ts}.db")
    gz_backup_path = os.path.join(BACKUP_DIR, f"hospital_db_{today}_{ts}_{trigger_mode}.db.gz")

    src_conn = sqlite3.connect(DB_PATH)
    dst_conn = sqlite3.connect(raw_temp_db)
    with dst_conn:
        src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    with open(raw_temp_db, "rb") as f_in, gzip.open(gz_backup_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw_temp_db)

    with open(gz_backup_path, "rb") as f:
        sha256_hash = hashlib.sha256(f.read()).hexdigest()
    size_kb = round(os.path.getsize(gz_backup_path) / 1024, 2)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO backup_audit_logs (backup_date, trigger_mode, file_path, file_size_kb, sha256_hash)
            VALUES (?, ?, ?, ?, ?)
        """, (today, trigger_mode, gz_backup_path, size_kb, sha256_hash))
        conn.commit()
        cur.close()

    watchdog_state["alert_level"] = "COMPLETED"
    watchdog_state["message"] = f"Database safely backed up today ({trigger_mode}) — SHA256: {sha256_hash[:12]}..."
    return {"date": today, "mode": trigger_mode, "path": gz_backup_path, "size_kb": size_kb, "sha256": sha256_hash}

def trigger_backup_reminder(level):
    if not is_backed_up_today():
        watchdog_state["alert_level"] = level
        if level == "REMINDER_1600":
            watchdog_state["message"] = "4:00 PM REMINDER: Daily Local Server Backup has not been run yet. Please click 'Backup Now'."
        elif level == "URGENT_1730":
            watchdog_state["message"] = "5:30 PM URGENT WARNING: No manual backup detected! Auto-Failsafe Backup will execute at 6:00 PM."

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(trigger_backup_reminder, "cron", hour=16, minute=0, args=["REMINDER_1600"])
scheduler.add_job(trigger_backup_reminder, "cron", hour=17, minute=30, args=["URGENT_1730"])
scheduler.add_job(run_hot_backup, "cron", hour=18, minute=0, args=["AUTO_FAILSAFE_1800"])
scheduler.start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/v1/checkin", methods=["POST"])
@require_valid_license
def unified_checkin():
    data = request.json or {}
    method = data.get("method", "TOUCH_KIOSK")
    identifier = data.get("identifier", "UNKNOWN").strip()
    full_name = data.get("full_name", "Verified Patient").strip()
    department = data.get("department", "General Triage")
    priority = data.get("priority", "Standard")

    with get_db() as conn:
        cur = conn.cursor()
        # FIX 4: Count per-department per-day so each department starts at 001 independently.
        # G-001, D-001, L-001 etc. — no cross-department collisions.
        sql_dept_count = ("SELECT COUNT(*) FROM patient_queue WHERE DATE(created_at) = CURRENT_DATE AND department = %s"
                          if DATABASE_URL else
                          "SELECT COUNT(*) FROM patient_queue WHERE date(created_at) = date('now') AND department = ?")
        cur.execute(sql_dept_count, (department,))
        dept_count_today = cur.fetchone()[0] + 1
        prefix = department[0].upper()
        ticket_no = f"{prefix}-{dept_count_today:03d}"

        # FIX 6: Check for duplicate check-in (same identifier, same department, same day)
        sql_dup = ("SELECT id FROM patient_queue WHERE identifier = %s AND department = %s AND DATE(created_at) = CURRENT_DATE AND status NOT IN ('SERVED') LIMIT 1"
                   if DATABASE_URL else
                   "SELECT id FROM patient_queue WHERE identifier = ? AND department = ? AND date(created_at) = date('now') AND status NOT IN ('SERVED') LIMIT 1")
        cur.execute(sql_dup, (identifier, department))
        existing = cur.fetchone()
        if existing:
            cur.close()
            return jsonify({
                "status": "DUPLICATE",
                "error": f"This patient has already checked in for {department} today. Use a different department or wait until tomorrow."
            }), 409

        sql_insert = ("INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status) VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')"
                      if DATABASE_URL else
                      "INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status) VALUES (?, ?, ?, ?, ?, ?, 'PENDING')")

        cur.execute(sql_insert, (identifier, full_name, method, department, priority, ticket_no))
        conn.commit()
        cur.close()

    return jsonify({
        "status": "SUCCESS",
        "ticket_number": ticket_no,
        "full_name": full_name,
        "method": method,
        "department": department,
        "sheets_saved": 3
    })

@app.route("/api/v1/patient/lookup/<identifier>", methods=["GET"])
@require_valid_license
def patient_lookup(identifier):
    """Looks up patient name and category from previous visits in the database."""
    with get_db() as conn:
        cur = conn.cursor()
        sql_lookup = ("SELECT full_name, department FROM patient_queue WHERE identifier = %s ORDER BY id DESC LIMIT 1" 
                      if DATABASE_URL else 
                      "SELECT full_name, department FROM patient_queue WHERE identifier = ? ORDER BY id DESC LIMIT 1")
        cur.execute(sql_lookup, (identifier,))
        row = cur.fetchone()
        cur.close()

    if row:
        return jsonify({
            "status": "FOUND",
            "full_name": row["full_name"],
            "category": f"Registered ({row['department']})"
        })
    return jsonify({
        "status": "NEW_PATIENT",
        "full_name": f"Citizen ID: {identifier[-4:]}",
        "category": "New Walk-in Patient"
    })

@app.route("/api/v1/kiosk/checkin", methods=["POST"])
@require_valid_license
def kiosk_manual_checkin():
    """Validates or registers manual National ID / Insurance entry at the public Touch Kiosk."""
    data = request.json or {}
    national_id = data.get("national_id", "").strip()
    department = data.get("department", "General Consultation")

    if len(national_id) < 5:
        return jsonify({"status": "ERROR", "error": "Invalid National ID format."}), 400

    with get_db() as conn:
        cur = conn.cursor()
        # FIX 6: Duplicate guard — block same ID checking in to same department twice today
        sql_dup = ("SELECT id FROM patient_queue WHERE identifier = %s AND department = %s AND DATE(created_at) = CURRENT_DATE AND status NOT IN ('SERVED') LIMIT 1"
                   if DATABASE_URL else
                   "SELECT id FROM patient_queue WHERE identifier = ? AND department = ? AND date(created_at) = date('now') AND status NOT IN ('SERVED') LIMIT 1")
        cur.execute(sql_dup, (national_id, department))
        if cur.fetchone():
            cur.close()
            return jsonify({"status": "DUPLICATE", "error": f"This ID has already checked in for {department} today."}), 409

        sql_find = "SELECT full_name FROM patient_queue WHERE identifier = %s LIMIT 1" if DATABASE_URL else "SELECT full_name FROM patient_queue WHERE identifier = ? LIMIT 1"
        cur.execute(sql_find, (national_id,))
        patient = cur.fetchone()

        # FIX 4: Per-department daily counter
        sql_dept_count = ("SELECT COUNT(*) FROM patient_queue WHERE DATE(created_at) = CURRENT_DATE AND department = %s"
                          if DATABASE_URL else
                          "SELECT COUNT(*) FROM patient_queue WHERE date(created_at) = date('now') AND department = ?")
        cur.execute(sql_dept_count, (department,))
        dept_count_today = cur.fetchone()[0] + 1
        prefix = department[0].upper()
        ticket_no = f"{prefix}-{dept_count_today:03d}"
        full_name = patient["full_name"] if patient else f"Citizen ID: {national_id[-4:]}"

        sql_insert = ("INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status) VALUES (%s, %s, 'KIOSK_MANUAL_ID', %s, 'Standard', %s, 'PENDING')"
                      if DATABASE_URL else
                      "INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status) VALUES (?, ?, 'KIOSK_MANUAL_ID', ?, 'Standard', ?, 'PENDING')")

        cur.execute(sql_insert, (national_id, full_name, department, ticket_no))
        conn.commit()
        cur.close()

    return jsonify({
        "status": "SUCCESS",
        "ticket_number": ticket_no,
        "full_name": full_name,
        "department": department,
        "sheets_saved": 3
    })

@app.route("/api/v1/dashboard", methods=["GET"])
def dashboard_stats():
    with get_db() as conn:
        cur = conn.cursor()
        
        sql_queue = "SELECT * FROM patient_queue WHERE status = 'WAITING' ORDER BY id DESC LIMIT 15"
        cur.execute(sql_queue)
        queue = [dict(r) for r in cur.fetchall()]

        sql_totals = "SELECT COUNT(*) as visits, COALESCE(SUM(sheets_saved), 0) as total_sheets FROM patient_queue"
        cur.execute(sql_totals)
        totals = cur.fetchone()

        sql_backups = "SELECT * FROM backup_audit_logs ORDER BY id DESC LIMIT 5"
        cur.execute(sql_backups)
        backups = [dict(r) for r in cur.fetchall()]
        cur.close()

    sheets = totals["total_sheets"] if totals else 0
    visits = totals["visits"] if totals else 0
    return jsonify({
        "queue": queue,
        "metrics": {
            "total_visits": visits,
            "sheets_saved": sheets,
            "trees_saved": round(sheets / 8333.0, 4),
            "co2_kg_saved": round(sheets * 0.0045, 2),
            "money_saved_usd": round(sheets * 0.05, 2)
        },
        "watchdog": {
            "backed_up_today": is_backed_up_today(),
            "alert_level": "COMPLETED" if is_backed_up_today() else watchdog_state["alert_level"],
            "message": watchdog_state["message"]
        },
        "backups": backups
    })

@app.route("/api/v1/queue/call-next", methods=["POST"])
@require_staff_pin
def call_next():
    # FIX: Only call patients who have already PAID into the doctor's room.
    # Workflow: Check-in (WAITING/PENDING) → Pay (WAITING/PAID) → Call (IN_CONSULTATION) → Consult → SERVED
    with get_db() as conn:
        cur = conn.cursor()
        sql_oldest = "SELECT id, ticket_number, full_name, department FROM patient_queue WHERE status = 'WAITING' AND payment_status = 'PAID' ORDER BY id ASC LIMIT 1"
        cur.execute(sql_oldest)
        oldest = cur.fetchone()

        if oldest:
            sql_update = "UPDATE patient_queue SET status = 'IN_CONSULTATION' WHERE id = %s" if DATABASE_URL else "UPDATE patient_queue SET status = 'IN_CONSULTATION' WHERE id = ?"
            cur.execute(sql_update, (oldest["id"],))
            conn.commit()
            cur.close()
            return jsonify({
                "status": "CALLED",
                "ticket": oldest["ticket_number"],
                "full_name": oldest["full_name"],
                "department": oldest["department"]
            })
        # Check if there are waiting patients who haven't paid yet
        sql_unpaid = "SELECT COUNT(*) FROM patient_queue WHERE status = 'WAITING' AND payment_status = 'PENDING'"
        cur.execute(sql_unpaid)
        unpaid_count = cur.fetchone()[0]
        cur.close()

    if unpaid_count > 0:
        return jsonify({"status": "AWAITING_PAYMENT", "message": f"{unpaid_count} patient(s) waiting but payment not yet confirmed."})
    return jsonify({"status": "EMPTY"})

@app.route("/api/v1/backup/run", methods=["POST"])
@require_staff_pin
def trigger_backup_api():
    mode = (request.json or {}).get("mode", "MANUAL_ADMIN")
    if mode == "SIMULATE_REMINDER":
        trigger_backup_reminder("URGENT_1730")
        return jsonify({"status": "REMINDER_TRIGGERED", "watchdog": watchdog_state})
    result = run_hot_backup(trigger_mode=mode)
    return jsonify({"status": "BACKUP_CREATED", "details": result})

# --- CASHIER BILLING ROUTES ---
@app.route("/cashier")
def cashier_portal():
    return render_template("cashier.html")

@app.route("/api/v1/cashier/pending", methods=["GET"])
def cashier_pending():
    with get_db() as conn:
        cur = conn.cursor()
        sql_pend = ("SELECT ticket_number, full_name, department, payment_status FROM patient_queue WHERE DATE(created_at) = CURRENT_DATE ORDER BY id DESC LIMIT 20" 
                    if DATABASE_URL else 
                    "SELECT ticket_number, full_name, department, payment_status FROM patient_queue WHERE date(created_at) = date('now') ORDER BY id DESC LIMIT 20")
        cur.execute(sql_pend)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    return jsonify(rows)

@app.route("/api/v1/cashier/lookup/<ticket_no>", methods=["GET"])
def cashier_lookup(ticket_no):
    with get_db() as conn:
        cur = conn.cursor()
        sql_lup = "SELECT ticket_number, full_name, department, identifier, payment_status FROM patient_queue WHERE ticket_number = %s ORDER BY id DESC LIMIT 1" if DATABASE_URL else "SELECT ticket_number, full_name, department, identifier, payment_status FROM patient_queue WHERE ticket_number = ? ORDER BY id DESC LIMIT 1"
        cur.execute(sql_lup, (ticket_no.upper(),))
        row = cur.fetchone()
        cur.close()
    if row:
        return jsonify({"status": "FOUND", "ticket": dict(row)})
    return jsonify({"status": "NOT_FOUND"})

@app.route("/api/v1/cashier/pay", methods=["POST"])
@require_staff_pin
def cashier_pay():
    data = request.json or {}
    ticket_no = data.get("ticket_number", "").upper()
    
    with get_db() as conn:
        cur = conn.cursor()
        sql_upd = "UPDATE patient_queue SET payment_status = 'PAID' WHERE ticket_number = %s" if DATABASE_URL else "UPDATE patient_queue SET payment_status = 'PAID' WHERE ticket_number = ?"
        cur.execute(sql_upd, (ticket_no,))
        
        sql_pat = "SELECT identifier, full_name, department FROM patient_queue WHERE ticket_number = %s LIMIT 1" if DATABASE_URL else "SELECT identifier, full_name, department FROM patient_queue WHERE ticket_number = ? LIMIT 1"
        cur.execute(sql_pat, (ticket_no,))
        patient = cur.fetchone()
        conn.commit()
        cur.close()

    if patient and (patient["identifier"].startswith("+") or len(patient["identifier"]) >= 10):
        try:
            send_payment_receipt(
                phone_number=patient["identifier"],
                patient_name=patient["full_name"],
                ticket_number=ticket_no,
                department=patient["department"]
            )
        except Exception as e:
            print(f"Notifier Error: {e}")

    return jsonify({"status": "SUCCESS", "ticket_number": ticket_no})

# --- TV DISPLAY ROUTES ---
# FIX 5: Register both /tv and /tv-display so neither returns 404
@app.route("/tv-display")
@app.route("/tv")
def tv_display():
    return render_template("tv_display.html")

@app.route("/api/v1/tv-status", methods=["GET"])
def tv_status():
    active_key = os.environ.get("ZEROPAPER_LICENSE_KEY", DEFAULT_DEV_KEY)
    license_info = verify_license_key(active_key)

    with get_db() as conn:
        cur = conn.cursor()
        # Show the patient currently IN_CONSULTATION (with the doctor right now).
        # Fall back to the most recently SERVED patient if no one is in consultation.
        sql_srv = "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'IN_CONSULTATION' ORDER BY id ASC LIMIT 1"
        cur.execute(sql_srv)
        now_serving = cur.fetchone()
        if not now_serving:
            sql_srv_fallback = "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'SERVED' ORDER BY id DESC LIMIT 1"
            cur.execute(sql_srv_fallback)
            now_serving = cur.fetchone()

        # Show all WAITING patients on the TV screen regardless of payment status,
        # so the waiting room display is populated as soon as patients check in.
        sql_nxt = "SELECT ticket_number, full_name, department, checkin_method, payment_status FROM patient_queue WHERE status = 'WAITING' ORDER BY id ASC LIMIT 5"
        cur.execute(sql_nxt)
        up_next = [dict(r) for r in cur.fetchall()]

        sql_tot = "SELECT COALESCE(SUM(sheets_saved), 0) as total_sheets FROM patient_queue"
        cur.execute(sql_tot)
        totals = cur.fetchone()
        cur.close()

    sheets = totals["total_sheets"] if totals else 0
    return jsonify({
        "now_serving": dict(now_serving) if now_serving else None,
        "up_next": up_next,
        "metrics": {
            "sheets_saved": sheets,
            "trees_saved": round(sheets / 8333.0, 4),
            "co2_kg_saved": round(sheets * 0.0045, 2)
        },
        "license": license_info
    })

# --- CLINICAL CONSULTATION & PHARMACY ROUTES ---
@app.route("/clinical")
def clinical_portal():
    return render_template("clinical.html")

@app.route("/kiosk")
def kiosk_touch_screen():
    return render_template("kiosk.html")

@app.route("/api/v1/clinical/waiting-tickets", methods=["GET"])
def get_waiting_tickets():
    # FIX: Show patients who have been called into consultation (IN_CONSULTATION status),
    # meaning they have paid and the receptionist has called them to the doctor's room.
    with get_db() as conn:
        cur = conn.cursor()
        sql_wt = "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'IN_CONSULTATION' ORDER BY id ASC"
        cur.execute(sql_wt)
        tickets = [dict(r) for r in cur.fetchall()]
        cur.close()
    return jsonify(tickets)

@app.route("/api/v1/clinical/save", methods=["POST"])
@require_staff_pin
def clinical_save():
    data = request.json or {}
    ticket = data.get("ticket_number")
    diagnosis = data.get("diagnosis")
    prescription = data.get("prescription")
    lab_notes = data.get("lab_notes", "None")

    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("""
                INSERT INTO clinical_records (ticket_number, diagnosis, prescription, lab_notes, status)
                VALUES (%s, %s, %s, %s, 'PENDING_PHARMACY')
                ON CONFLICT (id) DO UPDATE SET diagnosis = EXCLUDED.diagnosis, prescription = EXCLUDED.prescription, lab_notes = EXCLUDED.lab_notes
            """, (ticket, diagnosis, prescription, lab_notes))
            # Mark SERVED: consultation is complete, prescription sent to pharmacy
            cur.execute("UPDATE patient_queue SET status = 'SERVED' WHERE ticket_number = %s", (ticket,))
        else:
            cur.execute("""
                INSERT OR REPLACE INTO clinical_records (ticket_number, diagnosis, prescription, lab_notes, status)
                VALUES (?, ?, ?, ?, 'PENDING_PHARMACY')
            """, (ticket, diagnosis, prescription, lab_notes))
            # Mark SERVED: consultation is complete, prescription sent to pharmacy
            cur.execute("UPDATE patient_queue SET status = 'SERVED' WHERE ticket_number = ?", (ticket,))
        conn.commit()
        cur.close()

    return jsonify({"status": "SUCCESS", "message": "Prescription transmitted securely to hospital pharmacy."})

@app.route("/api/v1/pharmacy/pending", methods=["GET"])
def pharmacy_pending():
    with get_db() as conn:
        cur = conn.cursor()
        sql_ph = "SELECT * FROM clinical_records WHERE status = 'PENDING_PHARMACY' ORDER BY id DESC"
        cur.execute(sql_ph)
        records = [dict(r) for r in cur.fetchall()]
        cur.close()
    return jsonify(records)

@app.route("/api/v1/pharmacy/dispense", methods=["POST"])
@require_staff_pin
def pharmacy_dispense():
    data = request.json or {}
    record_id = data.get("record_id")
    with get_db() as conn:
        cur = conn.cursor()
        sql_dsp = "UPDATE clinical_records SET status = 'DISPENSED' WHERE id = %s" if DATABASE_URL else "UPDATE clinical_records SET status = 'DISPENSED' WHERE id = ?"
        cur.execute(sql_dsp, (record_id,))
        conn.commit()
        cur.close()
    return jsonify({"status": "DISPENSED"})

@app.route("/api/v1/export-esg-csv", methods=["GET"])
def export_esg_csv():
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["ZeroPaper OS - Certified ESG Sustainability & Financial Audit Report"])
    writer.writerow(["Export Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow([])

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as visits, COALESCE(SUM(sheets_saved), 0) as total_sheets FROM patient_queue")
        totals = cur.fetchone()
        cur.close()

    sheets = totals["total_sheets"] if totals else 0
    visits = totals["visits"] if totals else 0
    writer.writerow(["Environmental & Financial Metric", "Accumulated Total"])
    writer.writerow(["Total Patient Check-Ins", visits])
    writer.writerow(["Paper Sheets Eliminated", sheets])
    writer.writerow(["Full-Grown Trees Preserved", round(sheets / 8333.0, 4)])
    writer.writerow(["CO2 Emissions Prevented (kg)", round(sheets * 0.0045, 2)])
    writer.writerow(["Facility Printing Budget Saved (USD)", f"${round(sheets * 0.05, 2)}"])

    output.seek(0)
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=ZeroPaper_ESG_Sustainability_Report.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
