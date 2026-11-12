import os
import tempfile
import pytest
from datetime import datetime, timedelta

# Ensure testing mode before importing app modules
os.environ["ZEROPAPER_LICENSE_KEY"] = ""
os.environ["HOSPITAL_STAFF_PIN"] = "TEST_PIN_2026"
os.environ["ZEROPAPER_MASTER_SECRET"] = "test_master_secret_do_not_use_in_production_xyzabc"

from license_guard import generate_license_key, verify_license_key
import app as flask_app
import restore_backup

@pytest.fixture
def client():
    """Sets up a temporary isolated SQLite database and active test license for Flask tests."""
    db_fd, flask_app.DB_PATH = tempfile.mkstemp(suffix=".db")
    flask_app.app.config["TESTING"] = True
    flask_app.init_db()
    
    # Generate a valid active HMAC license key for testing
    valid_key = generate_license_key("TEST_HOSPITAL", (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
    os.environ["ZEROPAPER_LICENSE_KEY"] = valid_key

    with flask_app.app.test_client() as client:
        yield client

    os.close(db_fd)
    if os.path.exists(flask_app.DB_PATH):
        os.unlink(flask_app.DB_PATH)


# ==========================================
# 1. HMAC-SHA256 LICENSE GUARD TESTS
# ==========================================

def test_license_guard_valid():
    future_date = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")
    key = generate_license_key("KIGALI_GENERAL_HOSPITAL", future_date, "ENTERPRISE")
    result = verify_license_key(key)
    
    assert result["valid"] is True
    assert result["status"] == "ACTIVE"
    assert result["facility"] == "KIGALI_GENERAL_HOSPITAL"
    assert result["plan"] == "ENTERPRISE"
    assert result["days_remaining"] >= 14

def test_license_guard_expired():
    # Must be MORE than 7 days past expiry to leave the grace period window.
    # A 5-day-expired key is still within the 7-day hospital grace period (valid=True),
    # so the test must use >=8 days past expiry to reliably hit EXPIRED status.
    past_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    key = generate_license_key("EXPIRED_CLINIC", past_date)
    result = verify_license_key(key)

    assert result["valid"] is False
    assert result["status"] == "EXPIRED"

def test_license_guard_grace_period():
    """Keys expired within the last 7 days should still be valid (hospital grace period)."""
    past_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    key = generate_license_key("GRACE_CLINIC", past_date)
    result = verify_license_key(key)

    assert result["valid"] is True
    assert result["status"] == "GRACE_PERIOD"
    assert result["days_remaining"] < 0

def test_license_guard_tampered_signature():
    future_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    key = generate_license_key("SECURE_FACILITY", future_date)
    
    # Tamper with the cryptographic HMAC signature portion after the dot
    parts = key.split(".")
    tampered_key = parts[0] + ".deadbeefcafebabe99999999"
    result = verify_license_key(tampered_key)
    
    assert result["valid"] is False
    assert result["status"] == "INVALID_SIGNATURE"


# ==========================================
# 2. UNIFIED CHECK-IN ENDPOINT TESTS
# ==========================================

def test_unified_checkin_nfc(client):
    payload = {
        "method": "NFC_CARD",
        "identifier": "04:A9:3F:82",
        "full_name": "Jean-Paul Habimana",
        "department": "General Consultation",
        "priority": "Standard"
    }
    response = client.post("/api/v1/checkin", json=payload)
    assert response.status_code == 200
    
    data = response.get_json()
    assert data["status"] == "SUCCESS"
    assert data["ticket_number"].startswith("G-")
    assert data["sheets_saved"] == 3

def test_unified_checkin_ussd(client):
    payload = {
        "method": "USSD_SMS",
        "identifier": "+250722901114",
        "full_name": "USSD Mobile Caller",
        "department": "Laboratory & Diagnostics"
    }
    response = client.post("/api/v1/checkin", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["ticket_number"].startswith("L-")


# ==========================================
# 3. DASHBOARD, QUEUE & TV STATUS TESTS
# ==========================================

def test_dashboard_and_tv_feeds(client):
    # Perform two check-ins
    client.post("/api/v1/checkin", json={
        "method": "TOUCH_KIOSK",
        "identifier": "+250788412900",
        "full_name": "Aline Uwase",
        "department": "Pharmacy Dispatch"
    })
    client.post("/api/v1/checkin", json={
        "method": "ID_OCR",
        "identifier": "1199880045120981",
        "full_name": "Eric Mugisha",
        "department": "Dental Triage"
    })

    # Test Dashboard API
    dash_res = client.get("/api/v1/dashboard")
    assert dash_res.status_code == 200
    dash_data = dash_res.get_json()
    assert dash_data["metrics"]["total_visits"] == 2
    assert dash_data["metrics"]["sheets_saved"] == 6  # 2 visits * 3 sheets

    # Test TV Display Status API
    # up_next now shows ALL WAITING patients regardless of payment status (FIX 2)
    tv_res = client.get("/api/v1/tv-status")
    assert tv_res.status_code == 200
    tv_data = tv_res.get_json()
    assert len(tv_data["up_next"]) == 2
    assert tv_data["license"]["valid"] is True

def test_tv_route_alias(client):
    """FIX 5: Both /tv and /tv-display must return 200."""
    assert client.get("/tv-display").status_code == 200
    assert client.get("/tv").status_code == 200

def test_duplicate_checkin_blocked(client):
    """FIX 6: Same identifier checking into the same department twice today should return 409."""
    payload = {
        "method": "TOUCH_KIOSK",
        "identifier": "+250788000001",
        "full_name": "Duplicate Patient",
        "department": "General Consultation",
    }
    r1 = client.post("/api/v1/checkin", json=payload)
    assert r1.status_code == 200
    r2 = client.post("/api/v1/checkin", json=payload)
    assert r2.status_code == 409
    assert r2.get_json()["status"] == "DUPLICATE"

def test_ticket_numbering_per_department(client):
    """FIX 4: Ticket numbers must reset per department, not share a global counter."""
    client.post("/api/v1/checkin", json={"method":"TOUCH_KIOSK","identifier":"+250788000010","full_name":"P1","department":"General Consultation"})
    client.post("/api/v1/checkin", json={"method":"TOUCH_KIOSK","identifier":"+250788000011","full_name":"P2","department":"General Consultation"})
    r_dental = client.post("/api/v1/checkin", json={"method":"TOUCH_KIOSK","identifier":"+250788000012","full_name":"P3","department":"Dental Triage"})
    dental_data = r_dental.get_json()
    # Dental should start its own counter at D-001, not continue from G-002
    assert dental_data["ticket_number"] == "D-001", f"Expected D-001, got {dental_data['ticket_number']}"

def test_correct_workflow_order(client):
    """FIX 3: call-next must only call PAID patients; clinical list shows IN_CONSULTATION patients."""
    # Check in a patient
    client.post("/api/v1/checkin", json={"method":"TOUCH_KIOSK","identifier":"+250788000020","full_name":"Workflow Test","department":"General Consultation"})

    # call-next should NOT call an unpaid patient
    r = client.post("/api/v1/queue/call-next", headers={"X-Staff-PIN": "TEST_PIN_2026"})
    data = r.get_json()
    assert data["status"] in ("AWAITING_PAYMENT", "EMPTY"), f"Unpaid patient was called: {data}"

    # Pay the patient
    client.post("/api/v1/cashier/pay", json={"ticket_number": "G-001"}, headers={"X-Staff-PIN": "TEST_PIN_2026"})

    # Now call-next should work
    r2 = client.post("/api/v1/queue/call-next", headers={"X-Staff-PIN": "TEST_PIN_2026"})
    assert r2.get_json()["status"] == "CALLED"

    # Clinical waiting list should now contain the patient
    r3 = client.get("/api/v1/clinical/waiting-tickets")
    tickets = r3.get_json()
    assert len(tickets) == 1
    assert tickets[0]["ticket_number"] == "G-001"


# ==========================================
# 4. BACKUP PRUNING & RETENTION TESTS
# ==========================================

def test_backup_retention_pruning():
    with tempfile.TemporaryDirectory() as tmpdir:
        restore_backup.BACKUP_DIR = tmpdir
        
        # Create 35 dummy compressed backup files (exceeding MAX_RETENTION_DAYS = 30)
        for i in range(35):
            filepath = os.path.join(tmpdir, f"hospital_db_2026-06-{i+1:02d}_snapshot.db.gz")
            with open(filepath, "wb") as f:
                f.write(b"compressed sqlite backup payload bytes")

        # Run pruning
        removed_count = restore_backup.prune_old_backups()
        
        # Exactly 5 old backups should be pruned to maintain the 30-day rolling window
        assert removed_count == 5
        
        remaining_files = os.listdir(tmpdir)
        assert len(remaining_files) == 30
