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

# Stored strictly on the Client/Hospital's own Local Server or Data Center
DB_PATH = os.environ.get("HOSPITAL_DB_PATH", "hospital_local_server.db")
BACKUP_DIR = os.environ.get("HOSPITAL_BACKUP_DIR", "./local_server_backups")
STAFF_PIN = os.environ.get("HOSPITAL_STAFF_PIN", "2026")

# Global Watchdog State for UI Banner Reminders
watchdog_state = {
    "alert_level": "NORMAL",  # NORMAL | REMINDER_1600 | URGENT_1730 | COMPLETED
    "message": "Daily backup schedule active (4:00 PM Reminder / 6:00 PM Auto-FailSafe)."
}

def get_db():
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
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                full_name TEXT NOT NULL,
                checkin_method TEXT NOT NULL, -- 'NFC_CARD', 'TOUCH_KIOSK', 'USSD_SMS', 'ID_OCR', 'KIOSK_MANUAL_ID'
                department TEXT NOT NULL,
                priority TEXT DEFAULT 'Standard',
                ticket_number TEXT NOT NULL,
                status TEXT DEFAULT 'WAITING',
                payment_status TEXT DEFAULT 'PENDING', -- PENDING or PAID
                sheets_saved INTEGER DEFAULT 3,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_date TEXT NOT NULL,
                trigger_mode TEXT NOT NULL,   -- 'MANUAL_ADMIN' or 'AUTO_FAILSAFE_1800'
                file_path TEXT NOT NULL,
                file_size_kb REAL NOT NULL,
                sha256_hash TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def is_backed_up_today():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM backup_audit_logs WHERE backup_date = ? LIMIT 1", (today,)
        ).fetchone()
        return row is not None

def run_hot_backup(trigger_mode="AUTO_FAILSAFE_1800"):
    """Uses SQLite Online Backup API + GZIP compression + SHA-256 Checksum."""
    today = datetime.now().strftime("%Y-%m-%d")
    if trigger_mode == "AUTO_FAILSAFE_1800" and is_backed_up_today():
        return None  # Already backed up manually by Admin today

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
        conn.execute("""
            INSERT INTO backup_audit_logs (backup_date, trigger_mode, file_path, file_size_kb, sha256_hash)
            VALUES (?, ?, ?, ?, ?)
        """, (today, trigger_mode, gz_backup_path, size_kb, sha256_hash))
        conn.commit()

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
        cur.execute("SELECT COUNT(*) FROM patient_queue WHERE date(created_at) = date('now')")
        count_today = cur.fetchone()[0] + 1
        prefix = department[0].upper()
        ticket_no = f"{prefix}-{count_today:03d}"

        cur.execute("""
            INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
        """, (identifier, full_name, method, department, priority, ticket_no))
        conn.commit()

    return jsonify({
        "status": "SUCCESS",
        "ticket_number": ticket_no,
        "full_name": full_name,
        "method": method,
        "department": department,
        "sheets_saved": 3
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
        patient = cur.execute(
            "SELECT full_name FROM patient_queue WHERE identifier = ? LIMIT 1", (national_id,)
        ).fetchone()
        full_name = patient["full_name"] if patient else f"Citizen ID: {national_id[-4:]}"

        cur.execute("SELECT COUNT(*) FROM patient_queue WHERE date(created_at) = date('now')")
        count_today = cur.fetchone()[0] + 1
        prefix = department[0].upper()
        ticket_no = f"{prefix}-{count_today:03d}"

        cur.execute("""
            INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number, payment_status)
            VALUES (?, ?, 'KIOSK_MANUAL_ID', ?, 'Standard', ?, 'PENDING')
        """, (national_id, full_name, department, ticket_no))
        conn.commit()

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
        queue = [dict(r) for r in conn.execute(
            "SELECT * FROM patient_queue WHERE status = 'WAITING' ORDER BY id DESC LIMIT 15"
        ).fetchall()]

        totals = conn.execute("""
            SELECT COUNT(*) as visits, COALESCE(SUM(sheets_saved), 0) as total_sheets
            FROM patient_queue
        """).fetchone()

        backups = [dict(r) for r in conn.execute(
            "SELECT * FROM backup_audit_logs ORDER BY id DESC LIMIT 5"
        ).fetchall()]

    sheets = totals["total_sheets"]
    return jsonify({
        "queue": queue,
        "metrics": {
            "total_visits": totals["visits"],
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
    with get_db() as conn:
        oldest = conn.execute(
            "SELECT id, ticket_number FROM patient_queue WHERE status = 'WAITING' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if oldest:
            conn.execute("UPDATE patient_queue SET status = 'SERVED' WHERE id = ?", (oldest["id"],))
            conn.commit()
            return jsonify({"status": "CALLED", "ticket": oldest["ticket_number"]})
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
        rows = [dict(r) for r in conn.execute(
            "SELECT ticket_number, full_name, department, payment_status FROM patient_queue WHERE date(created_at) = date('now') ORDER BY id DESC LIMIT 20"
        ).fetchall()]
    return jsonify(rows)

@app.route("/api/v1/cashier/lookup/<ticket_no>", methods=["GET"])
def cashier_lookup(ticket_no):
    with get_db() as conn:
        row = conn.execute(
            "SELECT ticket_number, full_name, department, identifier, payment_status FROM patient_queue WHERE ticket_number = ? ORDER BY id DESC LIMIT 1",
            (ticket_no.upper(),)
        ).fetchone()
    if row:
        return jsonify({"status": "FOUND", "ticket": dict(row)})
    return jsonify({"status": "NOT_FOUND"})

@app.route("/api/v1/cashier/pay", methods=["POST"])
def cashier_pay():
    """Confirms cashier payment, updates status to PAID, and triggers SMS receipt notification."""
    data = request.json or {}
    ticket_no = data.get("ticket_number", "").upper()
    
    with get_db() as conn:
        conn.execute("UPDATE patient_queue SET payment_status = 'PAID' WHERE ticket_number = ?", (ticket_no,))
        patient = conn.execute(
            "SELECT identifier, full_name, department FROM patient_queue WHERE ticket_number = ? LIMIT 1",
            (ticket_no,)
        ).fetchone()
        conn.commit()

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
@app.route("/tv-display")
def tv_display():
    return render_template("tv_display.html")

@app.route("/api/v1/tv-status", methods=["GET"])
def tv_status():
    active_key = os.environ.get("ZEROPAPER_LICENSE_KEY", DEFAULT_DEV_KEY)
    license_info = verify_license_key(active_key)

    with get_db() as conn:
        now_serving = conn.execute(
            "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'SERVED' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        up_next = [dict(r) for r in conn.execute(
            "SELECT ticket_number, department, checkin_method FROM patient_queue WHERE status = 'WAITING' AND payment_status = 'PAID' ORDER BY id ASC LIMIT 5"
        ).fetchall()]

        totals = conn.execute(
            "SELECT COALESCE(SUM(sheets_saved), 0) as total_sheets FROM patient_queue"
        ).fetchone()

    sheets = totals["total_sheets"]
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
    """Only fetches waiting tickets where consultation fee payment is verified (PAID)."""
    with get_db() as conn:
        tickets = [dict(r) for r in conn.execute(
            "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'WAITING' AND payment_status = 'PAID' ORDER BY id ASC"
        ).fetchall()]
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
        conn.execute("""
            INSERT OR REPLACE INTO clinical_records (ticket_number, diagnosis, prescription, lab_notes, status)
            VALUES (?, ?, ?, ?, 'PENDING_PHARMACY')
        """, (ticket, diagnosis, prescription, lab_notes))
        conn.execute("UPDATE patient_queue SET status = 'IN_CONSULTATION' WHERE ticket_number = ?", (ticket,))
        conn.commit()

    return jsonify({"status": "SUCCESS", "message": "Prescription transmitted securely to hospital pharmacy."})

@app.route("/api/v1/pharmacy/pending", methods=["GET"])
def pharmacy_pending():
    with get_db() as conn:
        records = [dict(r) for r in conn.execute(
            "SELECT * FROM clinical_records WHERE status = 'PENDING_PHARMACY' ORDER BY id DESC"
        ).fetchall()]
    return jsonify(records)

@app.route("/api/v1/pharmacy/dispense", methods=["POST"])
@require_staff_pin
def pharmacy_dispense():
    data = request.json or {}
    record_id = data.get("record_id")
    with get_db() as conn:
        conn.execute("UPDATE clinical_records SET status = 'DISPENSED' WHERE id = ?", (record_id,))
        conn.commit()
    return jsonify({"status": "DISPENSED"})

@app.route("/api/v1/export-esg-csv", methods=["GET"])
def export_esg_csv():
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["ZeroPaper OS - Certified ESG Sustainability & Financial Audit Report"])
    writer.writerow(["Export Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow([])

    with get_db() as conn:
        totals = conn.execute("""
            SELECT COUNT(*) as visits, COALESCE(SUM(sheets_saved), 0) as total_sheets
            FROM patient_queue
        """).fetchone()

    sheets = totals["total_sheets"]
    writer.writerow(["Environmental & Financial Metric", "Accumulated Total"])
    writer.writerow(["Total Patient Check-Ins", totals["total_visits"]])
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
