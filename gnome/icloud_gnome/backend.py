"""Everything the GTK front end needs from icloud-linux, minus the widgets.

The driver itself lives in a virtualenv next to this package; this module
runs on the system Python and only talks to it through files (config,
status.json, the state database), systemd and the auth helper process.
"""
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import gi

gi.require_version("Secret", "1")
from gi.repository import Gio, GLib, GObject, Secret

import yaml

BACKEND_DIR = Path(os.environ.get("ICLOUD_LINUX_HOME") or Path(__file__).resolve().parents[2])
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"
ICLOUDCTL = BACKEND_DIR / "icloudctl"
AUTH_HELPER = BACKEND_DIR / "gnome" / "auth_helper.py"

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "icloud-linux"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / "icloud.env"
SERVICE_FILE = HOME / ".config" / "systemd" / "user" / "icloud.service"
PHOTOS_SERVICE_FILE = HOME / ".config" / "systemd" / "user" / "icloud-photos.service"
PHOTOS_ENV_FILE = CONFIG_DIR / "photos.env"
STATE_DIR = HOME / ".local" / "state" / "icloud-linux"
STATUS_FILE = STATE_DIR / "status.json"
PHOTOS_STATUS_FILE = STATE_DIR / "photos-status.json"
LOG_FILE = STATE_DIR / "icloud.log"
DEFAULT_CACHE_DIR = HOME / ".cache" / "icloud-linux"
DEFAULT_COOKIE_DIR = CONFIG_DIR / "cookies"
DEFAULT_MOUNT = HOME / "iCloud"
DEFAULT_PHOTOS_MOUNT = HOME / "iCloud Photos"
BOOKMARKS_FILE = HOME / ".config" / "gtk-3.0" / "bookmarks"

UNIT = "icloud.service"
PHOTOS_UNIT = "icloud-photos.service"

# Same attributes the driver passes to `secret-tool lookup`.
SECRET_SCHEMA = Secret.Schema.new(
    "io.github.jridder.ICloudLinux",
    Secret.SchemaFlags.DONT_MATCH_NAME,
    {"service": Secret.SchemaAttributeType.STRING, "username": Secret.SchemaAttributeType.STRING},
)


# ── config ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "cache_dir": str(DEFAULT_CACHE_DIR),
    "cookie_dir": str(DEFAULT_COOKIE_DIR),
    "warmup_mode": "background",
    "conflict_mode": "copy",
    "upload_interval_seconds": 30,
    "remote_refresh_interval_seconds": 300,
    "warmup_workers": 1,
    "auto_sync": True,
    "fuse_options": {"allow_other": False, "ro": False, "nonempty": True},
}


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        config = {}
    return {**DEFAULT_CONFIG, **config}


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".yaml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# Managed by the iCloud Drive app; hand edits are kept but comments are not.\n")
        yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
    os.replace(tmp, CONFIG_FILE)


def update_config(**changes):
    config = load_config()
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    save_config(config)
    return config


def is_configured():
    return bool(load_config().get("username")) and SERVICE_FILE.exists()


def _env_value(env_file, name, default):
    """A variable from an env file icloudctl wrote with printf %q."""
    if not env_file.exists():
        return default
    try:
        out = subprocess.run(
            ["bash", "-c", f'source "$1" && printf %s "${name}"', "bash", str(env_file)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    return Path(out) if out else default


def mount_dir():
    return _env_value(ENV_FILE, "ICLOUD_MOUNT", DEFAULT_MOUNT)


def photos_mount_dir():
    return _env_value(PHOTOS_ENV_FILE, "ICLOUD_PHOTOS_MOUNT", DEFAULT_PHOTOS_MOUNT)


def mirror_dir():
    return Path(os.path.expanduser(load_config()["cache_dir"])) / "mirror"


def icloud_path_for(local_path):
    """Map a folder picked under the mount (or the cache mirror) to an iCloud path."""
    local = Path(local_path).resolve()
    for root in (mount_dir(), mirror_dir()):
        try:
            rel = local.relative_to(root.resolve())
        except ValueError:
            continue
        return "/" + str(rel) if str(rel) != "." else "/"
    return None


# ── keyring ─────────────────────────────────────────────────────────────────

def _secret_attrs(username):
    return {"service": "icloud-linux", "username": username}


def store_password(username, password):
    Secret.password_store_sync(
        SECRET_SCHEMA, _secret_attrs(username), Secret.COLLECTION_DEFAULT,
        f"iCloud Drive ({username})", password, None,
    )


def lookup_password(username):
    return Secret.password_lookup_sync(SECRET_SCHEMA, _secret_attrs(username), None)


def clear_password(username):
    Secret.password_clear_sync(SECRET_SCHEMA, _secret_attrs(username), None)


# ── status ──────────────────────────────────────────────────────────────────

def read_status(path=STATUS_FILE):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def read_photos_status():
    return read_status(PHOTOS_STATUS_FILE)


def file_counts():
    """(downloaded, total) files known to the sync database, or None."""
    db = Path(os.path.expanduser(load_config()["cache_dir"])) / "state.sqlite3"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1) as conn:
            total, hydrated = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hydrated), 0) FROM entries WHERE type = 'file' AND tombstone = 0"
            ).fetchone()
        return hydrated, total
    except sqlite3.Error:
        return None


def disk_usage(path):
    """Bytes actually allocated under *path* (placeholders are sparse)."""
    try:
        out = subprocess.run(["du", "-s", "--block-size=1", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
        return int(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 0


def tail_log(lines=400):
    try:
        with open(LOG_FILE, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# ── systemd ─────────────────────────────────────────────────────────────────

class Service(GObject.Object):
    """Tracks one systemd user unit (icloud.service by default) over D-Bus."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, unit=UNIT):
        super().__init__()
        self.unit = unit
        self.load_state = "not-found"
        self.active_state = "inactive"
        self.unit_file_state = ""
        self.main_pid = 0
        self._unit = None
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.manager = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", None,
        )
        self.manager.call_sync("Subscribe", None, Gio.DBusCallFlags.NONE, -1, None)
        self.manager.connect("g-signal", self._on_manager_signal)
        self.refresh()

    def _on_manager_signal(self, _proxy, _sender, signal, _params):
        if signal in ("UnitFilesChanged", "Reloading"):
            self.refresh()

    def refresh(self):
        if self._unit is None:
            try:
                path = self.manager.call_sync(
                    "LoadUnit", GLib.Variant("(s)", (self.unit,)), Gio.DBusCallFlags.NONE, -1, None
                ).unpack()[0]
            except GLib.Error:
                return
            self._unit = Gio.DBusProxy.new_sync(
                self.bus, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.systemd1", path, "org.freedesktop.systemd1.Unit", None,
            )
            self._service = Gio.DBusProxy.new_sync(
                self.bus, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.systemd1", path, "org.freedesktop.systemd1.Service", None,
            )
            self._unit.connect("g-properties-changed", lambda *_: self._read())
            self._service.connect("g-properties-changed", lambda *_: self._read())
        self._read()

    def _read(self):
        if self._unit is None:
            return

        def prop(proxy, name, default):
            value = proxy.get_cached_property(name)
            return value.unpack() if value is not None else default

        self.load_state = prop(self._unit, "LoadState", "not-found")
        self.active_state = prop(self._unit, "ActiveState", "inactive")
        self.unit_file_state = prop(self._unit, "UnitFileState", "")
        self.main_pid = prop(self._service, "MainPID", 0)
        self.emit("changed")

    @property
    def running(self):
        return self.active_state in ("active", "activating", "reloading")

    @property
    def installed(self):
        return self.load_state == "loaded"

    @property
    def enabled(self):
        return self.unit_file_state == "enabled"

    def _call(self, method, args, callback=None):
        def done(proxy, result):
            try:
                proxy.call_finish(result)
                error = None
            except GLib.Error as exc:
                error = Gio.DBusError.strip_remote_error(exc) or exc.message
            if callback:
                callback(error)

        self.manager.call(method, args, Gio.DBusCallFlags.NONE, -1, None, done)

    def daemon_reload(self, callback=None):
        # UnitFileState is only re-read (and re-sent) after a reload.
        self._call("Reload", None, lambda error: (self.refresh(), callback and callback(error)))

    def start(self, callback=None):
        self._call("StartUnit", GLib.Variant("(ss)", (self.unit, "replace")), callback)

    def stop(self, callback=None):
        self._call("StopUnit", GLib.Variant("(ss)", (self.unit, "replace")), callback)

    def restart(self, callback=None):
        self._call("RestartUnit", GLib.Variant("(ss)", (self.unit, "replace")), callback)

    def set_enabled(self, enabled, callback=None):
        def done(error):
            if error:
                callback and callback(error)
            else:
                self.daemon_reload(callback)

        if enabled:
            self._call("EnableUnitFiles", GLib.Variant("(asbb)", ([self.unit], False, True)), done)
        else:
            self._call("DisableUnitFiles", GLib.Variant("(asb)", ([self.unit], False)), done)

    def sync_now(self, callback=None):
        # SIGUSR1 = sync now (Drive: upload, then crawl; Photos: rescan).
        self._call("KillUnit", GLib.Variant("(ssi)", (self.unit, "main", 10)), callback)


def run_icloudctl(args, callback):
    """Run icloudctl asynchronously; callback(ok, output)."""
    proc = Gio.Subprocess.new(
        [str(ICLOUDCTL), *args],
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE,
    )

    def done(p, result):
        _ok, out, _err = p.communicate_utf8_finish(result)
        callback(p.get_successful(), out or "")

    proc.communicate_utf8_async(None, None, done)


# ── setup helpers ───────────────────────────────────────────────────────────

def add_bookmark(path, label="iCloud Drive"):
    uri = Gio.File.new_for_path(str(path)).get_uri()
    BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = BOOKMARKS_FILE.read_text().splitlines() if BOOKMARKS_FILE.exists() else []
    if any(line.split(" ", 1)[0] == uri for line in lines):
        return
    lines.append(f"{uri} {label}")
    BOOKMARKS_FILE.write_text("\n".join(lines) + "\n")


def remove_bookmark(path):
    if not BOOKMARKS_FILE.exists():
        return
    uri = Gio.File.new_for_path(str(path)).get_uri()
    lines = [line for line in BOOKMARKS_FILE.read_text().splitlines() if line.split(" ", 1)[0] != uri]
    BOOKMARKS_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


def forget_session():
    """Drop saved cookies so the next start needs a fresh sign-in."""
    cookie_dir = Path(os.path.expanduser(load_config()["cookie_dir"]))
    if cookie_dir.is_dir():
        shutil.rmtree(cookie_dir)


# ── sign-in ─────────────────────────────────────────────────────────────────

class AuthSession(GObject.Object):
    """Drives gnome/auth_helper.py; emits 'event' with each JSON message."""

    __gsignals__ = {"event": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, username, password, accept_terms=False):
        super().__init__()
        self.proc = Gio.Subprocess.new(
            [str(VENV_PYTHON), "-u", str(AUTH_HELPER)],
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE,
        )
        self.stdin = self.proc.get_stdin_pipe()
        self.stdout = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.finished = False
        self.send({
            "username": username,
            "password": password,
            "cookie_dir": load_config()["cookie_dir"],
            "accept_terms": accept_terms,
        })
        self._read_next()

    def send(self, message):
        self.stdin.write_all((json.dumps(message) + "\n").encode(), None)
        self.stdin.flush(None)

    def submit_code(self, code):
        self.send({"code": code})

    def request_sms(self):
        self.send({"action": "sms"})

    def cancel(self):
        if not self.finished:
            self.finished = True
            self.proc.force_exit()

    def _read_next(self):
        self.stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream, result):
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if self.finished:
            return
        if line is None:
            self.finished = True
            self.emit("event", {"event": "error", "kind": "crashed",
                                "message": "The sign-in helper stopped unexpectedly."})
            return
        try:
            message = json.loads(line)
        except ValueError:
            self._read_next()
            return
        if message.get("event") == "ok" or (
            message.get("event") == "error" and message.get("kind") != "sms"
        ):
            self.finished = True
        self.emit("event", message)
        if not self.finished:
            self._read_next()
