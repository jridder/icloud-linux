#!/usr/bin/env python3
"""
auth_helper.py — non-interactive Apple ID sign-in for the GNOME front end.

Runs inside the icloud-linux virtualenv and speaks JSON lines on
stdin/stdout so the GTK app (system Python) can drive the 2FA flow
without a terminal. The password arrives on stdin, never in argv.

GUI -> helper (first line):
  {"username": ..., "password": ..., "cookie_dir": ..., "accept_terms": false}
GUI -> helper (afterwards):
  {"code": "123456"}          submit a verification code
  {"action": "sms"}           ask for a text message instead
helper -> GUI:
  {"event": "progress", "message": ...}
  {"event": "need_code", "method": "trusted_device" | "sms" | ..., "phone": ...}
  {"event": "code_invalid"}
  {"event": "ok", "apple_id": ...}
  {"event": "error", "kind": ..., "message": ...}
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import get_partition  # noqa: E402
from pyicloud import PyiCloudService  # noqa: E402
from pyicloud.exceptions import (  # noqa: E402
    PyiCloud2FARequiredException,
    PyiCloudAcceptTermsException,
    PyiCloudAccountLockedException,
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
)

DEBUG_LOG = os.path.expanduser("~/.local/state/icloud-linux/signin-debug.log")
# Response fields that help explain a refusal; nothing secret.
SAFE_FIELDS = ("authType", "authenticationType", "hasError", "serviceErrors", "service_errors",
               "securityCode", "trustedDeviceCount", "mode", "noTrustedDevices", "status")


def emit(event, **fields):
    sys.stdout.write(json.dumps({"event": event, **fields}) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)  # GUI went away (dialog cancelled)
    return json.loads(line)


def log_response(step, exc):
    """Record what Apple answered (status, endpoint, safe fields) for diagnosis."""
    response = getattr(exc, "response", None)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "step": step,
             "error": f"{type(exc).__name__}: {exc}", "code": getattr(exc, "code", None)}
    if response is not None:
        entry["status"] = response.status_code
        entry["endpoint"] = response.url.split("?", 1)[0] if response.url else None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            entry["keys"] = sorted(body)
            entry.update({k: body[k] for k in SAFE_FIELDS if k in body})
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        fd = os.open(DEBUG_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def phone_hint(api):
    """Apple's masked trusted number, e.g. "+1 (•••) •••-••12"."""
    phone = getattr(api, "_auth_data", {}).get("trustedPhoneNumber") or {}
    return phone.get("numberWithDialCode") or phone.get("obfuscatedNumber")


def verify(api):
    try:
        api.request_2fa_code()
    except Exception as exc:
        emit("progress", message=f"Could not trigger code delivery: {exc}")

    if api.two_factor_delivery_method == "security_key":
        emit("error", kind="security_key",
             message="This Apple ID uses a hardware security key, which icloud-linux cannot use yet.")
        sys.exit(1)

    emit("need_code", method=api.two_factor_delivery_method, phone=phone_hint(api))
    while True:
        msg = receive()
        if msg.get("action") == "sms":
            try:
                api._request_sms_2fa_code()
            except Exception as exc:
                emit("error", kind="sms", message=f"Apple would not send a text message: {exc}")
                continue
            emit("need_code", method="sms", phone=phone_hint(api))
            continue

        code = str(msg.get("code", "")).strip()
        try:
            ok = api.validate_2fa_code(code)
        except Exception:
            ok = False
        if ok:
            break
        emit("code_invalid")

    if not api.is_trusted_session:
        emit("progress", message="Trusting this computer…")
        api.trust_session()


def sign_in(api):
    """authenticate(), tolerating Apple's 409 on the 2FA-options request.

    After a correct password Apple sometimes answers the follow-up "which 2FA
    methods?" request with a 409 pyicloud doesn't classify, so it escapes as
    PyiCloudAPIResponseException even though 2FA has started. Retry that
    request once; without it, verify() still asks for a device code.
    """
    try:
        api.authenticate(force_refresh=True)
    except PyiCloud2FARequiredException:
        return
    except PyiCloudAPIResponseException as exc:
        log_response("authenticate", exc)
        if str(exc.code) != "409" or not getattr(api, "_requires_mfa", False):
            raise
    time.sleep(1)
    try:
        api._auth_data = api._get_mfa_auth_options()
    except PyiCloud2FARequiredException:
        pass
    except PyiCloudAPIResponseException as exc:
        log_response("2fa-options", exc)


def main():
    request = receive()
    username = request["username"]
    cookie_dir = os.path.expanduser(request.get("cookie_dir") or "~/.config/icloud-linux/cookies")
    # Sign in from a clean session; the old one only goes once this succeeds,
    # so a failed attempt leaves the running services' session alone.
    work_dir = cookie_dir.rstrip("/") + ".signin"
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)

    try:
        emit("progress", message="Contacting Apple…")
        partition = get_partition()
        api = PyiCloudService(
            username,
            request["password"],
            cookie_directory=work_dir,
            accept_terms=bool(request.get("accept_terms")),
            authenticate=False,
        )
        if partition:
            api._setup_endpoint = f"https://p{partition}-setup.icloud.com/setup/ws/1"

        emit("progress", message="Signing in…")
        sign_in(api)

        if api.requires_2fa:
            verify(api)

        if api.requires_2fa:
            emit("error", kind="incomplete", message="Apple still requires verification for this session.")
            sys.exit(1)
    except PyiCloudAccountLockedException as exc:
        log_response("authenticate", exc)
        emit("error", kind="locked",
             message="Apple says this Apple Account is locked. Unlock it at iforgot.apple.com, then try again.")
        sys.exit(1)
    except PyiCloudFailedLoginException as exc:
        log_response("authenticate", exc)
        emit("error", kind="bad_credentials", message=str(exc) or "Apple ID or password is incorrect.")
        sys.exit(1)
    except PyiCloudAcceptTermsException as exc:
        emit("error", kind="terms", message=str(exc))
        sys.exit(1)
    except Exception as exc:
        log_response("other", exc)
        emit("error", kind="other", message=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
    finally:
        if sys.exc_info()[0] is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

    old_dir = cookie_dir.rstrip("/") + ".old"
    shutil.rmtree(old_dir, ignore_errors=True)
    if os.path.isdir(cookie_dir):
        os.rename(cookie_dir, old_dir)
    os.rename(work_dir, cookie_dir)
    shutil.rmtree(old_dir, ignore_errors=True)

    apple_id = username
    if getattr(api, "data", None) and "dsInfo" in api.data:
        apple_id = api.data["dsInfo"].get("appleId", username)
    emit("ok", apple_id=apple_id)


if __name__ == "__main__":
    main()
