import os
import hmac
import json
import base64
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import jsonify

# SECURITY: The master signing secret MUST be set via environment variable.
# Never hardcode this value in source code — anyone with the repo can forge license keys.
# Generate a strong secret with: python -c "import secrets; print(secrets.token_hex(32))"
_raw_secret = os.environ.get("ZEROPAPER_MASTER_SECRET", "")
if not _raw_secret:
    import warnings
    warnings.warn(
        "ZEROPAPER_MASTER_SECRET is not set. Using a random ephemeral secret — "
        "license keys generated now will NOT be verifiable after restart. "
        "Set ZEROPAPER_MASTER_SECRET in your environment for production.",
        RuntimeWarning,
        stacklevel=2,
    )
    import secrets as _secrets
    _raw_secret = _secrets.token_hex(32)

MASTER_SIGNING_SECRET = _raw_secret

def generate_license_key(facility_id: str, expires_yyyy_mm_dd: str, plan: str = "ENTERPRISE") -> str:
    """Run this locally to issue a new signed license key for a paying hospital."""
    payload_dict = {
        "facility": facility_id,
        "expires": expires_yyyy_mm_dd,
        "plan": plan
    }
    payload_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    signature = hmac.new(
        MASTER_SIGNING_SECRET.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()[:24]

    return f"ZP-{payload_b64}.{signature}"

def verify_license_key(license_key: str) -> dict:
    """Verifies cryptographic authenticity and expiration date of the hospital's license."""
    if not license_key or not license_key.startswith("ZP-") or "." not in license_key:
        return {"valid": False, "status": "MISSING_OR_MALFORMED", "days_remaining": 0}

    try:
        raw_body = license_key[3:]  # Strip 'ZP-' prefix
        payload_b64, provided_sig = raw_body.split(".", 1)

        expected_sig = hmac.new(
            MASTER_SIGNING_SECRET.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()[:24]

        # Constant-time comparison prevents timing attacks
        if not hmac.compare_digest(provided_sig, expected_sig):
            return {"valid": False, "status": "INVALID_SIGNATURE", "days_remaining": 0}

        # Decode padded Base64 payload
        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        data = json.loads(payload_json)

        exp_date = datetime.strptime(data["expires"], "%Y-%m-%d").date()
        today = datetime.now().date()
        days_left = (exp_date - today).days

        if days_left >= 0:
            return {
                "valid": True,
                "status": "ACTIVE",
                "facility": data["facility"],
                "plan": data.get("plan", "ENTERPRISE"),
                "expires": data["expires"],
                "days_remaining": days_left
            }
        elif days_left >= -7:
            # 7-Day Emergency Grace Period for hospitals
            return {
                "valid": True,
                "status": "GRACE_PERIOD",
                "facility": data["facility"],
                "expires": data["expires"],
                "days_remaining": days_left
            }
        else:
            return {
                "valid": False,
                "status": "EXPIRED",
                "facility": data["facility"],
                "expires": data["expires"],
                "days_remaining": days_left
            }
    except Exception:
        return {"valid": False, "status": "CORRUPTED_KEY", "days_remaining": 0}

# Generate a default 30-day evaluation license if none is set in environment
DEFAULT_DEV_KEY = generate_license_key(
    "KIGALI_CENTRAL_HOSPITAL",
    (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
)

def require_valid_license(f):
    """Flask route decorator that blocks check-ins if the hospital's license is expired."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        active_key = os.environ.get("ZEROPAPER_LICENSE_KEY", DEFAULT_DEV_KEY)
        check = verify_license_key(active_key)
        if not check["valid"]:
            return jsonify({
                "status": "LICENSE_LOCKED",
                "error": f"ZeroPaper OS Subscription {check['status']}. Please renew your license key.",
                "details": check
            }), 402
        return f(*args, **kwargs)
    return decorated_function

if __name__ == "__main__":
    # Run `python license_guard.py` in terminal to generate keys for new clients
    sample_key = generate_license_key("KING_FAISAL_HOSPITAL", "2027-12-31", "ENTERPRISE_UNLIMITED")
    print("\n--- GENERATED CLIENT LICENSE KEY ---")
    print(sample_key)
    print("Verification Result:", verify_license_key(sample_key))
