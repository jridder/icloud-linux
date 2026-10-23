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
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import get_partition  # noqa: E402
from pyicloud import PyiCloudService  # noqa: E402
from pyicloud.exceptions import (  # noqa: E402
    PyiCloud2FARequiredException,
    PyiCloudAcceptTermsException,
    PyiCloudFailedLoginException,
)


def emit(event, **fields):
    sys.stdout.write(json.dumps({"event": event, **fields}) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)  # GUI went away (dialog cancelled)
    return json.loads(line)


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


def main():
    request = receive()
    username = request["username"]
    cookie_dir = os.path.expanduser(request.get("cookie_dir") or "~/.config/icloud-linux/cookies")
    os.makedirs(cookie_dir, exist_ok=True)

    try:
        emit("progress", message="Contacting Apple…")
        partition = get_partition()
        api = PyiCloudService(
            username,
            request["password"],
            cookie_directory=cookie_dir,
            accept_terms=bool(request.get("accept_terms")),
            authenticate=False,
        )
        if partition:
            api._setup_endpoint = f"https://p{partition}-setup.icloud.com/setup/ws/1"

        emit("progress", message="Signing in…")
        try:
            api.authenticate(force_refresh=True)
        except PyiCloud2FARequiredException:
            pass

        if api.requires_2fa:
            verify(api)

        if api.requires_2fa:
            emit("error", kind="incomplete", message="Apple still requires verification for this session.")
            sys.exit(1)
    except PyiCloudFailedLoginException as exc:
        emit("error", kind="bad_credentials", message=str(exc) or "Apple ID or password is incorrect.")
        sys.exit(1)
    except PyiCloudAcceptTermsException as exc:
        emit("error", kind="terms", message=str(exc))
        sys.exit(1)
    except Exception as exc:
        emit("error", kind="other", message=f"{type(exc).__name__}: {exc}")
        sys.exit(1)

    apple_id = username
    if getattr(api, "data", None) and "dsInfo" in api.data:
        apple_id = api.data["dsInfo"].get("appleId", username)
    emit("ok", apple_id=apple_id)


if __name__ == "__main__":
    main()
