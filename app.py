import os
import gzip
import shutil
import sqlite3
import hashlib
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# --- [1] IMPORTED LICENSE GUARD AT THE TOP ---
from license_guard import require_valid_license, verify_license_key, DEFAULT_DEV_KEY

app = Flask(__name__)

# Stored strictly on the Client/Hospital's own Local Server or Data Center
DB_PATH = os.environ.get("HOSPITAL_DB_PATH", "hospital_local_server.db")
BACKUP_DIR = os.environ.get("HOSPITAL_BACKUP_DIR", "./local_server_backups")

# Global Watchdog State for UI Banner Reminders
watchdog_state = {
    "alert_level": "NORMAL",  # NORMAL | REMINDER_1600 | URGENT_1730 | COMPLETED
    "message": "Daily backup schedule active (4:00 PM Reminder / 6:00 PM Auto-FailSafe)."
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                full_name TEXT NOT NULL,
                checkin_method TEXT NOT NULL, -- 'NFC_CARD', 'TOUCH_KIOSK', 'USSD_SMS', 'ID_OCR'
                department TEXT NOT NULL,
                priority TEXT DEFAULT 'Standard',
                ticket_number TEXT NOT NULL,
                status TEXT DEFAULT 'WAITING',
                sheets_saved INTEGER DEFAULT 3,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # 1. Safe Online Hot-Backup (zero downtime while patients check in)
    src_conn = sqlite3.connect(DB_PATH)
    dst_conn = sqlite3.connect(raw_temp_db)
    with dst_conn:
        src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    # 2. Compress with GZIP (4:1 ratio) and remove raw temporary file
    with open(raw_temp_db, "rb") as f_in, gzip.open(gz_backup_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw_temp_db)

    # 3. Compute SHA-256 Checksum & File Size
    with open(gz_backup_path, "rb") as f:
        sha256_hash = hashlib.sha256(f.read()).hexdigest()
    size_kb = round(os.path.getsize(gz_backup_path) / 1024, 2)

    # 4. Record in Hospital's Local Audit Table
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

# Start Background Watchdog Scheduler
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(trigger_backup_reminder, "cron", hour=16, minute=0, args=["REMINDER_1600"])
scheduler.add_job(trigger_backup_reminder, "cron", hour=17, minute=30, args=["URGENT_1730"])
scheduler.add_job(run_hot_backup, "cron", hour=18, minute=0, args=["AUTO_FAILSAFE_1800"])
scheduler.start()

@app.route("/")
def index():
    return render_template("index.html")

# --- [2] PROTECTED CHECK-IN ROUTE WITH @require_valid_license ---
@app.route("/api/v1/checkin", methods=["POST"])
@require_valid_license
def unified_checkin():
    """Handles all 4 check-in channels: NFC_CARD, TOUCH_KIOSK, USSD_SMS, ID_OCR."""
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
            INSERT INTO patient_queue (identifier, full_name, checkin_method, department, priority, ticket_number)
            VALUES (?, ?, ?, ?, ?, ?)
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
    trees_saved = round(sheets / 8333.0, 4)
    co2_kg_saved = round(sheets * 0.0045, 2)
    money_saved_usd = round(sheets * 0.05, 2)

    return jsonify({
        "queue": queue,
        "metrics": {
            "total_visits": totals["visits"],
            "sheets_saved": sheets,
            "trees_saved": trees_saved,
            "co2_kg_saved": co2_kg_saved,
            "money_saved_usd": money_saved_usd
        },
        "watchdog": {
            "backed_up_today": is_backed_up_today(),
            "alert_level": "COMPLETED" if is_backed_up_today() else watchdog_state["alert_level"],
            "message": watchdog_state["message"]
        },
        "backups": backups
    })

@app.route("/api/v1/queue/call-next", methods=["POST"])
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
def trigger_backup_api():
    mode = (request.json or {}).get("mode", "MANUAL_ADMIN")
    if mode == "SIMULATE_REMINDER":
        trigger_backup_reminder("URGENT_1730")
        return jsonify({"status": "REMINDER_TRIGGERED", "watchdog": watchdog_state})
    result = run_hot_backup(trigger_mode=mode)
    return jsonify({"status": "BACKUP_CREATED", "details": result})

# --- [3] ADDED TV DISPLAY & TV STATUS ROUTES RIGHT BEFORE __main__ ---
@app.route("/tv-display")
def tv_display():
    """Renders the Waiting Room TV Display screen."""
    return render_template("tv_display.html")

@app.route("/api/v1/tv-status", methods=["GET"])
def tv_status():
    """Feeds live Now-Serving ticket, Up-Next queue, Eco-Math, and License status to the TV."""
    active_key = os.environ.get("ZEROPAPER_LICENSE_KEY", DEFAULT_DEV_KEY)
    license_info = verify_license_key(active_key)

    with get_db() as conn:
        now_serving = conn.execute(
            "SELECT ticket_number, full_name, department FROM patient_queue WHERE status = 'SERVED' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        up_next = [dict(r) for r in conn.execute(
            "SELECT ticket_number, department, checkin_method FROM patient_queue WHERE status = 'WAITING' ORDER BY id ASC LIMIT 5"
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

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
