#!/usr/bin/env python3
"""
photos.py — read-only iCloud Photos FUSE filesystem.

Layout:
  /Library/<year>/<year-month>/<file>     every photo and video, by date taken
  /Albums/<album>/<file>                  your albums (folders become directories),
                                          plus Favorites, Screenshots and Videos

Originals download when a file is opened (or all of them in the background
with photos_warmup_mode: background) and are kept under
~/.cache/icloud-linux/photos. Live Photos appear as the photo plus its .MOV.

Desktop thumbnailers that open a file whose original is not downloaded yet
are handed Apple's JPEG preview instead, so browsing a folder in Files shows
thumbnails without pulling down every original.

Runs as icloud-photos.service and reuses the session that `icloudctl auth`
(or the GNOME app) saved for icloud.service.
"""

import datetime
import errno
import hashlib
import logging
import os
import shutil
import signal
import sqlite3
import stat
import sys
import threading
import time
from collections import OrderedDict

import fuse
from fuse import Fuse

from driver import StatusFile, lookup_keyring_password, parse_config, serve_signals, stop_mounted

fuse.fuse_python_api = (0, 2)

IO_CHUNK_SIZE = 1024 * 1024
URL_MAX_AGE = 20 * 60  # CloudKit download URLs are short-lived
SMART_ALBUMS = ("Favorites", "Screenshots")
UNAUTH_RETRY_SECONDS = 600

# /proc/<pid>/comm (15 chars max) of processes that only want a preview.
THUMBNAILERS = (
    "glycin-thumbnai",
    "gst-video-thumb",
    "gdk-pixbuf-thum",
    "totem-video-thu",
    "ffmpegthumbnail",
    "gnome-thumbnail",
)
# Indexers would otherwise download the whole library for metadata.
INDEXERS = ("localsearch-ext", "tracker-extract", "localsearch-3", "tracker-miner-f")


# ── naming ──────────────────────────────────────────────────────────────────

def safe_component(name):
    name = name.replace("/", "∕").replace("\0", "").strip()
    return name if name not in ("", ".", "..") else "_"


def allocate_names(items):
    """Give each asset's files unique names within one directory.

    *items* is an iterable of asset rows (dicts with filename, live_name, ...),
    already in display order. Returns [(name, asset_id, kind)], where kind is
    "original" or "live". A Live Photo's photo and video keep a matching stem.
    """
    taken = set()
    out = []
    for asset in items:
        stem, ext = os.path.splitext(safe_component(asset["filename"]))
        live_ext = os.path.splitext(asset["live_name"])[1] if asset.get("live_name") else None
        candidate, n = stem, 1
        while True:
            names = [candidate + ext] + ([candidate + live_ext] if live_ext else [])
            if not any(name.lower() in taken for name in names):
                break
            n += 1
            candidate = f"{stem} ({n})"
        for name in names:
            taken.add(name.lower())
        out.append((names[0], asset["id"], "original"))
        if live_ext:
            out.append((names[1], asset["id"], "live"))
    return out


def build_tree(assets, albums):
    """Return {path: ("dir", [children]) | ("file", asset_id, kind)}.

    *assets* maps asset id -> row; *albums* is a list of (fullname, [asset ids]).
    """
    tree = {"/": ("dir", ["Library", "Albums"]), "/Library": ("dir", []), "/Albums": ("dir", [])}

    def ensure_dir(path):
        if path in tree:
            return
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        ensure_dir(parent)
        tree[path] = ("dir", [])
        tree[parent][1].append(name)

    def fill(directory, rows):
        ensure_dir(directory)
        for name, asset_id, kind in allocate_names(rows):
            tree[f"{directory}/{name}"] = ("file", asset_id, kind)
            tree[directory][1].append(name)

    ordered = sorted(assets.values(), key=lambda a: (a["taken"], a["id"]))
    by_month = OrderedDict()
    for asset in ordered:
        when = datetime.datetime.fromtimestamp(asset["taken"])
        by_month.setdefault(f"/Library/{when:%Y}/{when:%Y-%m}", []).append(asset)
    for directory, rows in by_month.items():
        fill(directory, rows)

    used = set()
    for fullname, ids in albums:
        parts = [safe_component(p) for p in fullname.split("/") if p]
        path = "/Albums/" + "/".join(parts or ["_"])
        base, n = path, 1
        while path.lower() in used:
            n += 1
            path = f"{base} ({n})"
        used.add(path.lower())
        rows = sorted((assets[i] for i in ids if i in assets), key=lambda a: (a["taken"], a["id"]))
        fill(path, rows)
    return tree


# ── index ───────────────────────────────────────────────────────────────────

class PhotoIndex:
    """SQLite copy of the library listing so restarts mount instantly."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    item_type TEXT,
                    taken REAL NOT NULL,
                    size INTEGER,
                    live_name TEXT,
                    live_size INTEGER
                );
                CREATE TABLE IF NOT EXISTS albums (
                    fullname TEXT NOT NULL,
                    asset_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30)

    def replace(self, assets, albums, sync_token):
        with self.lock, self._conn() as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM albums")
            conn.executemany(
                "INSERT INTO assets VALUES (:id, :filename, :item_type, :taken, :size, :live_name, :live_size)",
                assets.values(),
            )
            conn.executemany(
                "INSERT INTO albums VALUES (?, ?)",
                [(name, asset_id) for name, ids in albums for asset_id in ids],
            )
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('sync_token', ?)", (sync_token,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('scanned_at', ?)", (str(time.time()),))

    def load(self):
        with self.lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            assets = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM assets")}
            albums = OrderedDict()
            for name, asset_id in conn.execute("SELECT fullname, asset_id FROM albums ORDER BY rowid"):
                albums.setdefault(name, []).append(asset_id)
        return assets, list(albums.items())

    def get_meta(self, key):
        with self.lock, self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


# ── filesystem ──────────────────────────────────────────────────────────────

class Stat(fuse.Stat):
    def __init__(self, mode, size=0, mtime=0, nlink=1):
        self.st_mode = mode
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = nlink
        self.st_uid = os.getuid()
        self.st_gid = os.getgid()
        self.st_size = size
        self.st_atime = self.st_mtime = self.st_ctime = int(mtime)


class ICloudPhotosFS(Fuse):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.logger = logging.getLogger("icloud-photos")
        self.api = None
        self.status = None
        self.tree = {"/": ("dir", ["Library", "Albums"]), "/Library": ("dir", []), "/Albums": ("dir", [])}
        self.assets = {}
        self.started = time.time()
        self.urls = {}
        self.urls_lock = threading.Lock()
        self.path_locks = {}
        self.path_locks_lock = threading.Lock()
        self.original_semaphore = threading.Semaphore(1)
        self.preview_semaphore = threading.Semaphore(3)
        self.caller_cache = {}
        self.refresh_event = threading.Event()
        self.stop_event = threading.Event()

    # setup ------------------------------------------------------------------

    def configure(self, config, cache_dir):
        self.cache_dir = cache_dir
        self.originals_dir = os.path.join(cache_dir, "originals")
        self.previews_dir = os.path.join(cache_dir, "previews")
        os.makedirs(self.originals_dir, exist_ok=True)
        os.makedirs(self.previews_dir, exist_ok=True)
        self.index = PhotoIndex(os.path.join(cache_dir, "index.sqlite3"))
        self.warmup_mode = config.get("photos_warmup_mode", "lazy")
        self.refresh_interval = int(config.get("photos_refresh_interval_seconds", 1800))
        self.auto_sync = bool(config.get("auto_sync", True))
        assets, albums = self.index.load()
        if assets:
            self._install(assets, albums)
            self.logger.info("Loaded %s photos from the local index", len(assets))

    def connect(self, username, password, cookie_dir, photos_cookie_dir):
        """Sign in with a copy of icloud.service's saved session."""
        from pyicloud import PyiCloudService
        import requests

        if os.path.isdir(cookie_dir):
            shutil.rmtree(photos_cookie_dir, ignore_errors=True)
            shutil.copytree(cookie_dir, photos_cookie_dir)
        os.makedirs(photos_cookie_dir, exist_ok=True)

        self.status.update(phase="connecting", auth_error=None)
        try:
            partition = requests.post(
                "https://setup.icloud.com/setup/ws/1/validate", json={}, timeout=10
            ).headers.get("x-apple-user-partition")
            api = PyiCloudService(username, password, cookie_directory=photos_cookie_dir, authenticate=False)
            if partition:
                api._setup_endpoint = f"https://p{partition}-setup.icloud.com/setup/ws/1"
            api.authenticate()
            if api.requires_2fa or api.requires_2sa:
                raise RuntimeError("iCloud session needs a new sign-in")
            self.api = api
            return True
        except Exception as exc:
            self.logger.error("Failed to connect to iCloud Photos: %s", exc)
            self.status.update(phase="unauthenticated", auth_error=str(exc) or type(exc).__name__)
            return False

    def fsinit(self):
        threading.Thread(target=self._sync_loop, name="photos-sync", daemon=True).start()

    def shutdown(self):
        self.stop_event.set()
        self.refresh_event.set()
        if self.status:
            self.status.update(phase="stopped")

    # crawl ------------------------------------------------------------------

    def _sync_loop(self):
        if self.api is None:
            # Sitting unauthenticated; the app restarts us after a new sign-in.
            self.status.update(phase="unauthenticated", photos=len(self.assets))
            return
        self.status.update(phase="ready" if self.assets else "scanning", photos=len(self.assets))
        while not self.stop_event.is_set():
            manual = self.refresh_event.is_set()
            self.refresh_event.clear()
            try:
                if manual or not self.assets or self._library_changed():
                    self.scan()
                self.status.update(force=True, last_refresh=time.time(), refreshing=False)
                if self.warmup_mode == "background":
                    self._warm_up()
            except Exception as exc:
                self.logger.exception("Photos refresh failed: %s", exc)
                self.status.update(force=True, refreshing=False, error=str(exc))
            wait = self.refresh_interval if self.auto_sync else None
            self.refresh_event.wait(wait)

    def _library_changed(self):
        token = self.index.get_meta("sync_token")
        if not token:
            return True
        for _event in self.api.photos.iter_changes(since=token):
            return True
        return False

    def scan(self):
        self.logger.info("Scanning iCloud Photos")
        self.status.update(force=True, refreshing=True, crawl={"photos": 0, "albums": 0})
        started = time.time()
        photos = self.api.photos
        token = photos.sync_cursor()
        assets = {}
        now = time.time()
        for asset in photos.all:
            row = self._asset_row(asset)
            if row is None:
                continue
            assets[row["id"]] = row
            self._remember_urls(asset, now)
            self.status.update(crawl={"photos": len(assets)})
            if self.stop_event.is_set():
                return

        albums = []
        for album in photos.albums:
            kind = type(album).__name__
            if kind == "PhotoAlbumFolder":
                continue
            if kind == "SmartPhotoAlbum" and album.name not in SMART_ALBUMS:
                continue
            name = album.name if kind == "SmartPhotoAlbum" else album.fullname
            albums.append((name, [asset.id for asset in album]))
            self.status.update(crawl={"albums": len(albums)})
        videos = [a["id"] for a in assets.values() if a["item_type"] == "movie"]
        if videos:
            albums.append(("Videos", videos))
        albums.sort(key=lambda item: item[0].lower())

        self.index.replace(assets, albums, token)
        self._install(assets, albums)
        self.logger.info(
            "Photos scan complete: %s items, %s albums in %.1fs",
            len(assets), len(albums), time.time() - started,
        )
        self.status.update(force=True, phase="ready", photos=len(assets))

    @staticmethod
    def _asset_row(asset):
        resources = asset.resources
        original = resources.get("original")
        if original is None:
            return None
        live = resources.get("original_video") if asset.is_live_photo else None
        taken = asset.asset_date or asset.created
        return {
            "id": asset.id,
            "filename": asset.filename or original.filename,
            "item_type": asset.item_type,
            "taken": taken.timestamp(),
            "size": original.size or 0,
            "live_name": live.filename if live else None,
            "live_size": (live.size or 0) if live else None,
        }

    def _remember_urls(self, asset, when):
        urls = {key: res.url for key, res in asset.resources.items() if res.url}
        with self.urls_lock:
            self.urls[asset.id] = (when, urls)

    def _install(self, assets, albums):
        tree = build_tree(assets, albums)
        self.assets, self.tree = assets, tree

    # downloads --------------------------------------------------------------

    def _cache_path(self, asset_id, kind):
        digest = hashlib.sha1(asset_id.encode()).hexdigest()
        if kind == "preview":
            return os.path.join(self.previews_dir, digest[:2], digest + ".jpg")
        return os.path.join(self.originals_dir, digest[:2], f"{digest}.{kind}")

    def _resource_url(self, asset_id, key):
        with self.urls_lock:
            cached = self.urls.get(asset_id)
        if cached and time.time() - cached[0] < URL_MAX_AGE and key in cached[1]:
            return cached[1][key]
        asset = self.api.photos.all.get(asset_id)
        if asset is None:
            raise FileNotFoundError(asset_id)
        self._remember_urls(asset, time.time())
        return {k: r.url for k, r in asset.resources.items()}.get(key)

    def _path_lock(self, key):
        with self.path_locks_lock:
            return self.path_locks.setdefault(key, threading.Lock())

    def ensure_cached(self, asset_id, kind):
        """Download one resource if needed; return its local path."""
        target = self._cache_path(asset_id, kind)
        if os.path.exists(target):
            return target
        if self.api is None:
            raise PermissionError("not signed in")
        asset = self.assets[asset_id]
        if kind == "preview":
            keys = ("medium_image", "thumb_image") if asset["item_type"] == "movie" else ("medium", "thumb")
            semaphore = self.preview_semaphore
        else:
            keys = ("original_video",) if kind == "live" else ("original",)
            semaphore = self.original_semaphore
        with self._path_lock((asset_id, kind)):
            if os.path.exists(target):
                return target
            url = next((u for u in (self._resource_url(asset_id, k) for k in keys) if u), None)
            if url is None:
                raise FileNotFoundError(f"{asset_id} has no {kind}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = f"{target}.part"
            with semaphore:
                self.logger.info("Downloading %s %s (%s)", kind, asset["filename"], asset_id)
                response = self.api.session.get(url, stream=True, timeout=60)
                response.raise_for_status()
                with open(tmp, "wb") as handle:
                    for chunk in response.iter_content(IO_CHUNK_SIZE):
                        handle.write(chunk)
            os.utime(tmp, (asset["taken"], asset["taken"]))
            os.replace(tmp, target)
        return target

    def _warm_up(self):
        pending = [
            (a["id"], kind)
            for a in sorted(self.assets.values(), key=lambda a: -a["taken"])
            for kind in (("original", "live") if a["live_name"] else ("original",))
            if not os.path.exists(self._cache_path(a["id"], kind))
        ]
        total, done = len(pending), 0
        self.status.update(force=True, hydration={"completed": 0, "total": total})
        for asset_id, kind in pending:
            if self.stop_event.is_set() or self.refresh_event.is_set():
                return
            try:
                self.ensure_cached(asset_id, kind)
            except Exception as exc:
                self.logger.error("Background download failed for %s: %s", asset_id, exc)
            done += 1
            self.status.update(force=done == total, hydration={"completed": done, "total": total})

    # callers ----------------------------------------------------------------

    def _caller_role(self):
        """'thumbnailer', 'indexer' or 'user' for the process behind this request."""
        try:
            pid = self.GetContext()["pid"]
        except Exception:
            return "user"
        now = time.time()
        cached = self.caller_cache.get(pid)
        if cached and now - cached[0] < 30:
            return cached[1]
        role = classify_process(pid)
        if len(self.caller_cache) > 256:
            self.caller_cache.clear()
        self.caller_cache[pid] = (now, role)
        return role

    # FUSE -------------------------------------------------------------------

    def getattr(self, path):
        node = self.tree.get(path)
        if node is None:
            return -errno.ENOENT
        if node[0] == "dir":
            return Stat(stat.S_IFDIR | 0o555, nlink=2, mtime=self.started)
        asset = self.assets[node[1]]
        size = asset["live_size"] if node[2] == "live" else asset["size"]
        return Stat(stat.S_IFREG | 0o444, size=size or 0, mtime=asset["taken"])

    def readdir(self, path, offset):
        node = self.tree.get(path)
        if node is None or node[0] != "dir":
            return -errno.ENOENT
        return [fuse.Direntry(name) for name in [".", ".."] + node[1]]

    def open(self, path, flags):
        node = self.tree.get(path)
        if node is None or node[0] != "file":
            return -errno.ENOENT
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
            return -errno.EROFS
        _kind, asset_id, kind = node
        if os.path.exists(self._cache_path(asset_id, kind)):
            return 0
        role = self._caller_role()
        if role == "indexer":
            return -errno.EACCES
        try:
            if role == "thumbnailer":
                self.ensure_cached(asset_id, "preview")
                # Bypass the page cache so the preview never shadows the original.
                return fuse.FuseFileInfo(direct_io=True)
            self.ensure_cached(asset_id, kind)
        except PermissionError:
            return -errno.EACCES
        except Exception as exc:
            self.logger.error("Could not download %s: %s", path, exc)
            return -errno.EIO
        return 0

    def read(self, path, size, offset):
        node = self.tree.get(path)
        if node is None or node[0] != "file":
            return -errno.ENOENT
        _kind, asset_id, kind = node
        local = self._cache_path(asset_id, kind)
        if not os.path.exists(local):
            preview = self._cache_path(asset_id, "preview")
            if self._caller_role() == "thumbnailer" and os.path.exists(preview):
                local = preview
            else:
                try:
                    local = self.ensure_cached(asset_id, kind)
                except Exception as exc:
                    self.logger.error("Could not download %s: %s", path, exc)
                    return -errno.EIO
        with open(local, "rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    def statfs(self):
        st = os.statvfs(self.cache_dir)
        return st


def classify_process(pid):
    """Walk up a few parents: thumbnailers run sandboxed under bwrap."""
    for _ in range(8):
        try:
            with open(f"/proc/{pid}/comm") as handle:
                comm = handle.read().strip()
            with open(f"/proc/{pid}/stat") as handle:
                ppid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return "user"
        if comm.startswith(THUMBNAILERS):
            return "thumbnailer"
        if comm.startswith(INDEXERS):
            return "indexer"
        if ppid <= 1:
            return "user"
        pid = ppid
    return "user"


def main():
    fs = ICloudPhotosFS(version="%prog " + fuse.__version__, usage="%prog [options] mountpoint",
                        dash_s_do="setsingle")
    fs.parser.add_option("-c", "--config", dest="config",
                         default=os.path.expanduser("~/.config/icloud-linux/config.yaml"))
    fs.parse(errex=1)
    options = fs.cmdline[0]
    fs.fuse_args.add("ro")
    fs.fuse_args.add("fsname=icloud-photos")
    fs.fuse_args.add("subtype=icloud-photos")

    state_dir = os.path.expanduser("~/.local/state/icloud-linux")
    os.makedirs(state_dir, exist_ok=True)
    log_path = os.environ.get("ICLOUD_PHOTOS_LOG_PATH", os.path.join(state_dir, "photos.log"))
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
    )
    fs.status = StatusFile(os.environ.get("ICLOUD_PHOTOS_STATUS_PATH") or os.path.join(state_dir, "photos-status.json"))
    fs.status.update(phase="starting", mountpoint=fs.fuse_args.mountpoint)

    config = parse_config(options.config)
    username = config.get("username")
    if not username:
        fs.logger.error("Username not provided in config file")
        sys.exit(1)
    password = config.get("password") or lookup_keyring_password(username, fs.logger)
    cache_root = os.path.expanduser(config.get("cache_dir", "~/.cache/icloud-linux"))
    cookie_dir = os.path.expanduser(config.get("cookie_dir", "~/.config/icloud-linux/cookies"))

    fs.configure(config, os.path.join(cache_root, "photos"))
    fs.connect(username, password, cookie_dir, cookie_dir.rstrip("/") + "-photos")

    def handle_shutdown(signum):
        fs.logger.info("Received signal %s, shutting down", signum)
        stop_mounted(fs.fuse_args.mountpoint, fs.shutdown)

    serve_signals({
        signal.SIGTERM: handle_shutdown,
        signal.SIGINT: handle_shutdown,
        signal.SIGUSR1: lambda _signum: fs.refresh_event.set(),
    })
    try:
        fs.main()
    finally:
        fs.shutdown()


if __name__ == "__main__":
    main()
