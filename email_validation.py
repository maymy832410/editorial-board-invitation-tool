"""Email validation via Rapid Email Verifier (free, no auth)."""

import json
import urllib.parse
import urllib.request
from typing import Tuple

RAPID_VERIFIER_BASE = "https://rapid-email-verifier.fly.dev"
VALIDATE_PATH = "/api/validate"
TIMEOUT_SECONDS = 10

# Only these statuses allow sending (per plan)
ALLOWED_STATUSES = ("VALID", "PROBABLY_VALID")

# User-friendly messages for known statuses
STATUS_MESSAGES = {
    "INVALID": "Email is invalid.",
    "MISSING_EMAIL": "No email provided.",
    "INVALID_FORMAT": "Invalid email format.",
    "INVALID_DOMAIN": "Domain does not exist or cannot receive mail.",
    "NO_MX_RECORDS": "Domain has no mail servers (MX records).",
    "DISPOSABLE": "Disposable/temporary email addresses are not allowed.",
}


def validate_email_for_send(email: str) -> Tuple[bool, str]:
    """
    Validate an email address using Rapid Email Verifier.
    Allow send only when status is VALID or PROBABLY_VALID.

    Returns:
        (True, message) if valid for sending.
        (False, message) if invalid, error, or uncertain (do not send).
    """
    if not email or not email.strip():
        return False, "No email address provided."

    email = email.strip()
    if "@" not in email:
        return False, "Invalid email format."

    url = f"{RAPID_VERIFIER_BASE}{VALIDATE_PATH}?email={urllib.parse.quote(email)}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return False, "Validation failed (service error). Try again or skip this address."
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return False, f"Validation failed (HTTP {e.code}). Try again or skip this address."
    except urllib.error.URLError as e:
        return False, "Validation failed (network or service error). Try again or skip this address."
    except json.JSONDecodeError:
        return False, "Validation failed (invalid response). Try again or skip this address."
    except TimeoutError:
        return False, "Validation failed (timeout). Try again or skip this address."
    except Exception:
        return False, "Validation failed (network or service error). Try again or skip this address."

    status = data.get("status")
    if not status:
        return False, "Validation failed (no status in response). Try again or skip this address."

    if status in ALLOWED_STATUSES:
        return True, "Valid."

    msg = STATUS_MESSAGES.get(status)
    if msg is None:
        msg = f"Email not valid for sending: {status}."
    else:
        msg = f"Email not valid for sending: {msg}"
    return False, msg
