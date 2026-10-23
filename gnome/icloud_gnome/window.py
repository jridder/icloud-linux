import os
import threading
import time
from gettext import ngettext
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from . import backend

DOWNLOAD_MODES = [
    ("background", "Download everything", "Keep a full local copy of your iCloud Drive"),
    ("lazy", "Download when opened", "Only files you open use disk space"),
]
PHOTO_MODES = [
    ("lazy", "Download when opened", "Thumbnails come from Apple’s previews"),
    ("background", "Download everything", "Keep a full local copy of your photo library"),
]


def relative_time(timestamp):
    if not timestamp:
        return None
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "just now"
    if delta < 3600:
        minutes = delta // 60
        return ngettext("%d minute ago", "%d minutes ago", minutes) % minutes
    if delta < 86400:
        hours = delta // 3600
        return ngettext("%d hour ago", "%d hours ago", hours) % hours
    return time.strftime("%b %-d, %H:%M", time.localtime(timestamp))


def summarize(service, status, config):
    """(icon, title, subtitle, progress fraction or None, style class)."""
    if not service.running:
        if service.active_state == "failed":
            return ("dialog-error-symbolic", "iCloud Drive Stopped Unexpectedly",
                    "Check the log for details, then turn it back on.", None, "error")
        if service.active_state == "deactivating":
            return ("content-loading-symbolic", "Stopping…", None, None, "dimmed")
        return ("io.github.jridder.ICloudLinux-off-symbolic", "iCloud Drive Is Off",
                "Turn it on to access your files.", None, "dimmed")

    if status.get("pid") != service.main_pid:
        return ("content-loading-symbolic", "Starting…", None, None, "accent")

    phase = status.get("phase")
    crawl = status.get("crawl") or {}
    if phase == "unauthenticated" or status.get("auth_error"):
        return ("dialog-password-symbolic", "Sign-In Required",
                "Apple ended this session. Sign in again to keep syncing.", None, "warning")
    if phase in ("starting", "connecting"):
        return ("content-loading-symbolic", "Connecting to iCloud…", None, None, "accent")
    if phase == "scanning":
        entries = crawl.get("entries", 0)
        return ("io.github.jridder.ICloudLinux-syncing-symbolic", "Scanning iCloud Drive…",
                f"{entries:,} items found so far — folders fill in as they’re found", None, "accent")
    if phase == "reconciling":
        return ("io.github.jridder.ICloudLinux-syncing-symbolic", "Checking Downloaded Files…", None, None, "accent")
    if phase == "stopped":
        return ("content-loading-symbolic", "Stopping…", None, None, "dimmed")

    pending = status.get("pending_uploads") or 0
    if status.get("uploading"):
        return ("io.github.jridder.ICloudLinux-syncing-symbolic", "Uploading Changes…",
                ngettext("%d item left", "%d items left", pending) % pending, None, "accent")
    if status.get("refreshing"):
        return ("io.github.jridder.ICloudLinux-syncing-symbolic", "Checking for Changes…",
                f"{crawl.get('entries', 0):,} items checked", None, "accent")
    hydration = status.get("hydration") or {}
    done, total = hydration.get("completed", 0), hydration.get("total", 0)
    if total and done < total:
        return ("folder-download-symbolic", "Downloading Files…",
                f"{done:,} of {total:,}", done / total, "accent")
    if pending and not config.get("auto_sync", True):
        return ("io.github.jridder.ICloudLinux-syncing-symbolic",
                ngettext("%d Change Waiting to Upload", "%d Changes Waiting to Upload", pending) % pending,
                "Automatic sync is off. Use Sync Now to upload.", None, "warning")
    when = relative_time(status.get("last_refresh"))
    return ("io.github.jridder.ICloudLinux-done-symbolic", "Up to Date", f"Last checked {when}" if when else None, None, "success")


def summarize_photos(service, status):
    """One-line state of icloud-photos.service for the Photos row."""
    if not service.running:
        return "Off" if service.active_state != "failed" else "Stopped unexpectedly — see the log"
    if status.get("pid") != service.main_pid:
        return "Starting…"
    phase = status.get("phase")
    crawl = status.get("crawl") or {}
    hydration = status.get("hydration") or {}
    if phase == "unauthenticated" or status.get("auth_error"):
        return "Sign-in required"
    if phase in ("starting", "connecting"):
        return "Connecting to iCloud…"
    if phase == "scanning" or status.get("refreshing"):
        return f"Scanning… {crawl.get('photos', 0):,} items"
    done, total = hydration.get("completed", 0), hydration.get("total", 0)
    if total and done < total:
        return f"Downloading {done:,} of {total:,}"
    return f"{status.get('photos', 0):,} items · read-only"


def page(title, content, tag=None, header_end=None):
    view = Adw.ToolbarView()
    header = Adw.HeaderBar()
    if header_end:
        header.pack_end(header_end)
    view.add_top_bar(header)
    view.set_content(content)
    nav = Adw.NavigationPage(title=title, child=view)
    if tag:
        nav.set_tag(tag)
    return nav


def pill(label, suggested=False, destructive=False):
    button = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
    button.add_css_class("pill")
    if suggested:
        button.add_css_class("suggested-action")
    if destructive:
        button.add_css_class("destructive-action")
    return button


# ── sign-in ─────────────────────────────────────────────────────────────────

class SignInFlow(GObject.Object):
    """Apple ID + password page, then a verification-code page if needed.

    Emits 'signed-in' (username, password) once the helper reports success.
    """

    __gsignals__ = {"signed-in": (GObject.SignalFlags.RUN_FIRST, None, (str, str))}

    def __init__(self, window, username=None):
        super().__init__()
        self.window = window
        self.session = None
        self.code_page = None
        self.accept_terms = False

        prefs = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Sign In With Your Apple ID",
            description="Your password is stored in GNOME Keyring. Apple will send a "
                        "verification code to one of your devices.",
        )
        prefs.add(group)
        self.email = Adw.EntryRow(title="Apple ID", input_purpose=Gtk.InputPurpose.EMAIL)
        if username:
            self.email.set_text(username)
            self.email.set_editable(False)
        self.password = Adw.PasswordEntryRow(title="Password")
        for row in (self.email, self.password):
            row.connect("changed", self._update_sensitive)
            row.connect("entry-activated", lambda *_: self._submit())
            group.add(row)

        self.error = Gtk.Label(wrap=True, xalign=0, margin_top=12, visible=False)
        self.error.add_css_class("error")
        group.add(self.error)

        actions = Adw.PreferencesGroup()
        self.button = pill("Sign In", suggested=True)
        self.button.set_margin_top(12)
        self.button.connect("clicked", lambda *_: self._submit())
        self.spinner = Adw.Spinner(visible=False, height_request=32)
        self.progress = Gtk.Label(visible=False)
        self.progress.add_css_class("dimmed")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for widget in (self.button, self.spinner, self.progress):
            box.append(widget)
        actions.add(box)
        prefs.add(actions)

        self.page = page("Sign In", prefs, tag="sign-in")
        self.page.connect("hidden", self._on_hidden)
        self._update_sensitive()

    def _on_hidden(self, *_):
        # Backing out of the code page (or the flow) abandons the attempt.
        if self.session and self.window.nav.get_visible_page() is not self.code_page:
            self.session.cancel()
            self.session = None
            self._set_busy(False)
            self._update_sensitive()

    def _update_sensitive(self, *_):
        ready = bool(self.email.get_text().strip()) and bool(self.password.get_text())
        self.button.set_sensitive(ready and self.session is None)

    def _set_busy(self, busy, message=None):
        self.button.set_visible(not busy)
        self.spinner.set_visible(busy)
        self.progress.set_visible(busy and bool(message))
        self.progress.set_label(message or "")
        self.email.set_sensitive(not busy)
        self.password.set_sensitive(not busy)

    def _show_error(self, message):
        self.error.set_label(message or "")
        self.error.set_visible(bool(message))

    def _submit(self):
        if not self.button.get_sensitive():
            return
        self._show_error(None)
        self._set_busy(True, "Contacting Apple…")
        self.session = backend.AuthSession(
            self.email.get_text().strip(), self.password.get_text(), accept_terms=self.accept_terms,
        )
        self.session.connect("event", self._on_event)

    def _on_event(self, _session, message):
        event = message.get("event")
        if event == "progress":
            self._set_busy(True, message.get("message"))
            if self.code_page:
                self.code_page.set_busy(True, message.get("message"))
        elif event == "need_code":
            self._show_code_page(message.get("method"), message.get("phone"))
        elif event == "code_invalid":
            self.code_page.show_error("That code didn’t work. Check it and try again.")
        elif event == "ok":
            self.session = None
            self.emit("signed-in", self.email.get_text().strip(), self.password.get_text())
        elif event == "error":
            kind = message.get("kind")
            if kind == "sms":
                self.code_page.show_error(message.get("message"))
                return
            self.session = None
            self._set_busy(False)
            if self.window.nav.get_visible_page() is self.code_page:
                self.window.nav.pop_to_page(self.page)
            if kind == "terms":
                self._ask_terms()
                return
            if kind == "bad_credentials":
                self._show_error("Your Apple ID or password is incorrect.")
            else:
                self._show_error(message.get("message") or "Sign-in failed.")
            self._update_sensitive()

    def _ask_terms(self):
        dialog = Adw.AlertDialog(
            heading="Accept Updated Terms?",
            body="Apple requires you to accept updated iCloud terms and conditions "
                 "before this account can be used.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("accept", "Accept and Sign In")
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response):
            if response == "accept":
                self.accept_terms = True
                self._submit()
            else:
                self._update_sensitive()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _show_code_page(self, method, phone):
        if self.code_page is None:
            self.code_page = CodePage(self)
            self.code_page.connect("hidden", self._on_hidden)
        self.code_page.prepare(method, phone)
        if self.window.nav.get_visible_page() is not self.code_page:
            self.window.nav.push(self.code_page)


class CodePage(Adw.NavigationPage):
    def __init__(self, flow):
        super().__init__(title="Verification", tag="code")
        self.flow = flow
        self.status = Adw.StatusPage(icon_name="phone-symbolic", title="Enter Verification Code")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp = Adw.Clamp(maximum_size=320, child=box)
        self.status.set_child(clamp)

        self.entry = Gtk.Entry(
            input_purpose=Gtk.InputPurpose.DIGITS, max_length=6, xalign=0.5,
            placeholder_text="000000",
        )
        self.entry.add_css_class("title-1")
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("activate", lambda *_: self._verify())
        box.append(self.entry)

        self.error = Gtk.Label(wrap=True, visible=False, justify=Gtk.Justification.CENTER)
        self.error.add_css_class("error")
        box.append(self.error)

        self.verify = pill("Verify", suggested=True)
        self.verify.connect("clicked", lambda *_: self._verify())
        box.append(self.verify)
        self.spinner = Adw.Spinner(visible=False, height_request=32)
        box.append(self.spinner)

        self.sms = Gtk.Button(label="Text Me a Code Instead", halign=Gtk.Align.CENTER)
        self.sms.add_css_class("flat")
        self.sms.connect("clicked", self._request_sms)
        box.append(self.sms)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.status)
        self.set_child(view)

    def prepare(self, method, phone):
        if method == "sms":
            target = f" to {phone}" if phone else ""
            text = f"Apple sent a code by text message{target}."
        elif method == "trusted_device":
            text = "Tap Allow on one of your Apple devices, then enter the 6-digit code it shows."
        else:
            text = "Enter the 6-digit code shown on your Apple device or sent by text message."
        self.status.set_description(text)
        self.sms.set_visible(method != "sms")
        self.entry.set_text("")
        self.verify.set_sensitive(False)
        self.show_error(None)
        self.set_busy(False)
        GLib.idle_add(self._focus_entry)

    def _focus_entry(self):
        self.entry.grab_focus()
        return GLib.SOURCE_REMOVE  # grab_focus() returns True, which would repeat

    def set_busy(self, busy, _message=None):
        self.verify.set_visible(not busy)
        self.spinner.set_visible(busy)
        self.entry.set_sensitive(not busy)
        self.sms.set_sensitive(not busy)

    def show_error(self, message):
        self.set_busy(False)
        self.error.set_label(message or "")
        self.error.set_visible(bool(message))
        if message:
            self.entry.select_region(0, -1)
            self.entry.grab_focus()

    def _on_changed(self, entry):
        text = entry.get_text()
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits != text:
            entry.set_text(digits)
            return
        self.verify.set_sensitive(len(digits) == 6)
        if len(digits) == 6:
            GLib.idle_add(self._verify)

    def _verify(self):
        code = self.entry.get_text()
        if len(code) != 6 or self.spinner.get_visible() or self.flow.session is None:
            return
        self.error.set_visible(False)
        self.set_busy(True)
        self.flow.session.submit_code(code)

    def _request_sms(self, *_):
        if self.flow.session:
            self.set_busy(True)
            self.flow.session.request_sms()


# ── first-run setup ─────────────────────────────────────────────────────────

def welcome_page(on_start):
    status = Adw.StatusPage(
        icon_name="io.github.jridder.ICloudLinux",
        title="iCloud Drive",
        description="Use your iCloud Drive files on this computer. They appear as a "
                    "normal folder and stay in sync in the background.",
    )
    button = pill("Get Started", suggested=True)
    button.connect("clicked", lambda *_: on_start())
    status.set_child(button)
    return page("Welcome", status, tag="welcome")


class FolderPage(Adw.NavigationPage):
    __gsignals__ = {"finish": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, window):
        super().__init__(title="Choose Folder", tag="folder")
        self.window = window
        self.mount = backend.DEFAULT_MOUNT
        prefs = Adw.PreferencesPage()

        where = Adw.PreferencesGroup(title="Where Your Files Appear")
        self.mount_row = Adw.ActionRow(title="Folder", subtitle=str(self.mount), use_markup=False)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._choose)
        self.mount_row.add_suffix(change)
        where.add(self.mount_row)
        self.bookmark = Adw.SwitchRow(title="Show in Files Sidebar", active=True)
        where.add(self.bookmark)
        prefs.add(where)

        how = Adw.PreferencesGroup(title="Downloads")
        self.mode = Adw.ComboRow(title="Files on This Computer")
        self.mode.set_model(Gtk.StringList.new([title for _key, title, _ in DOWNLOAD_MODES]))
        self.mode.connect("notify::selected", self._update_mode_subtitle)
        how.add(self.mode)
        self.autostart = Adw.SwitchRow(title="Start at Login", active=True)
        how.add(self.autostart)
        prefs.add(how)
        self._update_mode_subtitle()

        finish = Adw.PreferencesGroup()
        self.button = pill("Finish Setup", suggested=True)
        self.button.connect("clicked", lambda *_: self.emit("finish"))
        finish.add(self.button)
        prefs.add(finish)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(prefs)
        self.set_child(view)

    @property
    def warmup_mode(self):
        return DOWNLOAD_MODES[self.mode.get_selected()][0]

    def _update_mode_subtitle(self, *_):
        self.mode.set_subtitle(DOWNLOAD_MODES[self.mode.get_selected()][2])

    def _choose(self, *_):
        dialog = Gtk.FileDialog(title="Choose a Folder for iCloud Drive", accept_label="Choose")
        dialog.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            path = Path(folder.get_path())
            if any(path.iterdir()):
                # Mounting over files would hide them.
                path = path / "iCloud Drive"
            self.mount = path
            self.mount_row.set_subtitle(str(path))

        dialog.select_folder(self.window, None, done)


def working_page(title):
    status = Adw.StatusPage(title=title, child=Adw.Spinner(height_request=48, width_request=48))
    nav = page(title, status, tag="working")
    nav.set_can_pop(False)
    return nav, status


# ── main page ───────────────────────────────────────────────────────────────

class PathListGroup(Adw.PreferencesGroup):
    """Editable list of iCloud paths backed by one config key."""

    def __init__(self, window, key, title, description, empty):
        super().__init__(title=title, description=description)
        self.window = window
        self.key = key
        self.empty = empty
        self.rows = []
        self.add_row = Adw.ButtonRow(title="Add Folder…", start_icon_name="list-add-symbolic")
        self.add_row.connect("activated", self._add)
        self.reload()

    def reload(self):
        for row in self.rows:
            self.remove(row)
        self.rows = []
        paths = backend.load_config().get(self.key) or []
        if not paths:
            row = Adw.ActionRow(title=self.empty)
            row.add_css_class("dimmed")
            self.rows.append(row)
        for path in paths:
            row = Adw.ActionRow(title=path, icon_name="folder-symbolic", use_markup=False)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", self._remove, path)
            row.add_suffix(remove)
            self.rows.append(row)
        self.rows.append(self.add_row)
        for row in self.rows:
            self.add(row)

    def _save(self, paths):
        backend.update_config(**{self.key: paths or None})
        self.reload()
        self.window.settings_changed()

    def _remove(self, _button, path):
        self._save([p for p in backend.load_config().get(self.key) or [] if p != path])

    def _add(self, *_):
        root = backend.mount_dir()
        if not os.path.ismount(root):
            root = backend.mirror_dir()  # service off: browse the cached tree
        dialog = Gtk.FileDialog(title="Choose an iCloud Drive Folder", accept_label="Add")
        dialog.set_initial_folder(Gio.File.new_for_path(str(root)))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            icloud_path = backend.icloud_path_for(folder.get_path())
            if icloud_path is None:
                self.window.toast("Choose a folder inside iCloud Drive.")
                return
            paths = backend.load_config().get(self.key) or []
            if icloud_path not in paths:
                self._save(paths + [icloud_path])

        dialog.select_folder(self.window, None, done)


class MainPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="iCloud Drive", tag="main")
        self.window = window
        self.service = window.service
        self.photos_service = window.photos_service
        self._syncing = False
        self._loading = True
        self._updating = False

        menu = Gio.Menu()
        menu.append("Show Log", "win.show-log")
        menu.append("About iCloud Drive", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                     tooltip_text="Main Menu", primary=True)

        self.banner = Adw.Banner(title="Restart iCloud Drive to apply your changes.",
                                 button_label="Restart")
        self.banner.connect("button-clicked", lambda *_: self._restart())

        prefs = Adw.PreferencesPage()
        prefs.add(self._build_status())
        prefs.add(self._build_service())
        prefs.add(self._build_photos())
        prefs.add(self._build_account())
        prefs.add(self._build_syncing())
        self.sync_paths = PathListGroup(
            window, "sync_paths", "Synced Folders",
            "Only these folders are downloaded and can be changed. Everything else stays read-only.",
            "All of iCloud Drive",
        )
        prefs.add(self.sync_paths)
        self.exclude_paths = PathListGroup(
            window, "exclude_paths", "Excluded Folders",
            "Never download or change these folders, even inside a synced folder.",
            "None",
        )
        prefs.add(self.exclude_paths)
        prefs.add(self._build_storage())

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(menu_button)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.set_content(prefs)
        self.set_child(view)

        self.service.connect("changed", lambda *_: self.refresh())
        self.photos_service.connect("changed", lambda *_: self.refresh())
        self.monitor = Gio.File.new_for_path(str(backend.STATE_DIR)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self._on_state_changed)
        self.tick = GLib.timeout_add_seconds(5, self._on_tick)
        self.connect("destroy", lambda *_: GLib.source_remove(self.tick))
        self.load_settings()
        self.refresh()
        self._refresh_storage()

    # status ---------------------------------------------------------------

    def _build_status(self):
        group = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=6)
        self.icon = Gtk.Image(pixel_size=64, margin_bottom=6)
        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.title.add_css_class("title-2")
        self.subtitle = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.subtitle.add_css_class("dimmed")
        self.progress = Gtk.ProgressBar(margin_top=6, margin_start=48, margin_end=48)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=12, homogeneous=True)
        self.open_button = pill("Open in Files", suggested=True)
        self.open_button.connect("clicked", lambda *_: self.window.open_folder())
        self.sync_button = pill("Sync Now")
        self.sync_button.connect("clicked", self._sync_now)
        self.signin_button = pill("Sign In…", suggested=True)
        self.signin_button.connect("clicked", lambda *_: self.window.reauthenticate())
        for button in (self.signin_button, self.open_button, self.sync_button):
            buttons.append(button)
        for widget in (self.icon, self.title, self.subtitle, self.progress, buttons):
            box.append(widget)
        group.add(box)
        return group

    def refresh(self):
        status = backend.read_status()
        config = backend.load_config()
        icon, title, subtitle, fraction, style = summarize(self.service, status, config)
        self.icon.set_from_icon_name(icon)
        for css in ("success", "accent", "warning", "error", "dimmed"):
            self.icon.remove_css_class(css)
        self.icon.add_css_class(style)
        self.title.set_label(title)
        self.subtitle.set_label(subtitle or "")
        self.subtitle.set_visible(bool(subtitle))
        self.progress.set_visible(fraction is not None)
        if fraction is not None:
            self.progress.set_fraction(fraction)

        fresh = self.service.running and status.get("pid") == self.service.main_pid
        needs_auth = fresh and (status.get("phase") == "unauthenticated" or bool(status.get("auth_error")))
        self.signin_button.set_visible(needs_auth)
        self.open_button.set_visible(not needs_auth)
        self.open_button.set_sensitive(fresh and status.get("phase") in ("ready", "scanning"))
        busy = status.get("refreshing") or status.get("uploading")
        if self._syncing and not busy and fresh:
            self._syncing = False
        self.sync_button.set_visible(not needs_auth)
        self.sync_button.set_sensitive(fresh and status.get("phase") == "ready" and not busy and not self._syncing)

        self._updating = True
        self.photos_row.set_active(self.photos_service.running)
        self.photos_row.set_subtitle(summarize_photos(self.photos_service, backend.read_photos_status()))
        self.photos_folder_row.set_subtitle(str(backend.photos_mount_dir()))
        for row in (self.photos_folder_row, self.photos_mode_row):
            row.set_sensitive(self.photos_service.installed)
        self.running_row.set_active(self.service.running)
        self.autostart_row.set_active(self.service.enabled)
        self._updating = False
        if not self.service.running:
            self.banner.set_revealed(False)

    def _on_state_changed(self, _monitor, file, _other, event):
        if file.get_basename() in ("status.json", "photos-status.json") and event in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.RENAMED,
        ):
            self.refresh()

    def _on_tick(self):
        if self.window.is_visible():
            self.refresh()
            self._refresh_counts()
        return GLib.SOURCE_CONTINUE

    def _sync_now(self, *_):
        self._syncing = True
        self.sync_button.set_sensitive(False)
        self.service.sync_now(lambda error: error and self.window.toast(f"Could not sync: {error}"))
        if self.photos_service.running:
            self.photos_service.sync_now()

    # service --------------------------------------------------------------

    def _build_service(self):
        group = Adw.PreferencesGroup(title="Service")
        self.running_row = Adw.SwitchRow(title="iCloud Drive", subtitle="Mount your files and keep them in sync")
        self.running_row.connect("notify::active", self._on_running_toggled)
        group.add(self.running_row)
        self.autostart_row = Adw.SwitchRow(title="Start at Login")
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        group.add(self.autostart_row)
        self.mount_row = Adw.ActionRow(title="Folder", subtitle=str(backend.mount_dir()), use_markup=False)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._change_mount)
        self.mount_row.add_suffix(change)
        group.add(self.mount_row)
        return group

    def _report(self, what):
        return lambda error: error and self.window.toast(f"Could not {what}: {error}")

    def _on_running_toggled(self, row, _pspec):
        if self._updating or row.get_active() == self.service.running:
            return
        if row.get_active():
            self.service.start(self._report("start iCloud Drive"))
        else:
            self.service.stop(self._report("stop iCloud Drive"))

    def _on_autostart_toggled(self, row, _pspec):
        if self._updating or row.get_active() == self.service.enabled:
            return
        self.service.set_enabled(row.get_active(), self._report("change login startup"))

    def _restart(self):
        self.banner.set_revealed(False)
        self.service.restart(self._report("restart iCloud Drive"))

    def _change_mount(self, *_):
        dialog = Gtk.FileDialog(title="Choose a Folder for iCloud Drive", accept_label="Choose")
        dialog.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            new = Path(folder.get_path())
            old = backend.mount_dir()
            if new == old:
                return
            if any(new.iterdir()):
                new = new / "iCloud Drive"
            self.window.move_mount(old, new)

        dialog.select_folder(self.window, None, done)

    # photos ---------------------------------------------------------------

    def _build_photos(self):
        group = Adw.PreferencesGroup(
            title="iCloud Photos",
            description="Browse your photo library as a read-only folder. Nothing on this "
                        "computer can change or delete your photos.",
        )
        self.photos_row = Adw.SwitchRow(title="Show iCloud Photos")
        self.photos_row.connect("notify::active", self._on_photos_toggled)
        group.add(self.photos_row)
        self.photos_folder_row = Adw.ActionRow(title="Folder", use_markup=False)
        open_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_button.connect("clicked", lambda *_: self.window.open_folder(backend.photos_mount_dir()))
        self.photos_folder_row.add_suffix(open_button)
        group.add(self.photos_folder_row)
        self.photos_mode_row = Adw.ComboRow(title="Photos on This Computer")
        self.photos_mode_row.set_model(Gtk.StringList.new([title for _key, title, _ in PHOTO_MODES]))
        self.photos_mode_row.connect("notify::selected", self._on_photos_mode)
        group.add(self.photos_mode_row)
        return group

    def _on_photos_toggled(self, row, _pspec):
        if self._updating or row.get_active() == self.photos_service.running:
            return
        if row.get_active():
            self.window.enable_photos()
        else:
            self.photos_service.set_enabled(False, lambda _e: self.photos_service.stop(
                self._report("stop iCloud Photos")))

    def _on_photos_mode(self, row, _pspec):
        row.set_subtitle(PHOTO_MODES[row.get_selected()][2])
        if self._loading:
            return
        backend.update_config(photos_warmup_mode=PHOTO_MODES[row.get_selected()][0])
        if self.photos_service.running:
            self.photos_service.restart(self._report("restart iCloud Photos"))

    # account --------------------------------------------------------------

    def _build_account(self):
        group = Adw.PreferencesGroup(title="Account")
        self.account_row = Adw.ActionRow(title="Apple ID", subtitle_selectable=True, use_markup=False)
        again = Gtk.Button(label="Sign In Again…", valign=Gtk.Align.CENTER)
        again.connect("clicked", lambda *_: self.window.reauthenticate())
        self.account_row.add_suffix(again)
        group.add(self.account_row)
        sign_out = Adw.ButtonRow(title="Sign Out…")
        sign_out.add_css_class("destructive-action")
        sign_out.connect("activated", lambda *_: self.window.sign_out())
        group.add(sign_out)
        return group

    # syncing --------------------------------------------------------------

    def _build_syncing(self):
        group = Adw.PreferencesGroup(title="Syncing")
        self.mode_row = Adw.ComboRow(title="Files on This Computer")
        self.mode_row.set_model(Gtk.StringList.new([title for _key, title, _ in DOWNLOAD_MODES]))
        self.mode_row.connect("notify::selected", self._on_mode)
        group.add(self.mode_row)
        self.auto_row = Adw.SwitchRow(title="Sync Automatically",
                                      subtitle="Off: only sync when you choose Sync Now")
        self.auto_row.connect("notify::active", self._on_auto)
        group.add(self.auto_row)
        self.refresh_row = Adw.SpinRow.new_with_range(1, 1440, 1)
        self.refresh_row.set_title("Check iCloud Every")
        self.refresh_row.set_subtitle("Minutes")
        self.refresh_row.connect("notify::value", self._on_interval)
        group.add(self.refresh_row)
        self.upload_row = Adw.SpinRow.new_with_range(5, 3600, 5)
        self.upload_row.set_title("Upload Changes Every")
        self.upload_row.set_subtitle("Seconds")
        self.upload_row.connect("notify::value", self._on_interval)
        group.add(self.upload_row)
        return group

    def load_settings(self):
        config = backend.load_config()
        self._loading = True
        self.account_row.set_subtitle(config.get("username") or "")
        modes = [key for key, _title, _ in DOWNLOAD_MODES]
        mode = config.get("warmup_mode")
        self.mode_row.set_selected(modes.index(mode) if mode in modes else 0)
        self.mode_row.set_subtitle(DOWNLOAD_MODES[self.mode_row.get_selected()][2])
        photo_modes = [key for key, _title, _ in PHOTO_MODES]
        photo_mode = config.get("photos_warmup_mode")
        self.photos_mode_row.set_selected(photo_modes.index(photo_mode) if photo_mode in photo_modes else 0)
        self.photos_mode_row.set_subtitle(PHOTO_MODES[self.photos_mode_row.get_selected()][2])
        self.auto_row.set_active(bool(config.get("auto_sync", True)))
        self.refresh_row.set_value(max(1, int(config.get("remote_refresh_interval_seconds", 300)) // 60))
        self.upload_row.set_value(int(config.get("upload_interval_seconds", 30)))
        self.refresh_row.set_sensitive(self.auto_row.get_active())
        self.upload_row.set_sensitive(self.auto_row.get_active())
        self.mount_row.set_subtitle(str(backend.mount_dir()))
        self.sync_paths.reload()
        self.exclude_paths.reload()
        self._loading = False

    def _on_mode(self, row, _pspec):
        row.set_subtitle(DOWNLOAD_MODES[row.get_selected()][2])
        if not self._loading:
            backend.update_config(warmup_mode=DOWNLOAD_MODES[row.get_selected()][0])
            self.window.settings_changed()

    def _on_auto(self, row, _pspec):
        self.refresh_row.set_sensitive(row.get_active())
        self.upload_row.set_sensitive(row.get_active())
        if not self._loading:
            backend.update_config(auto_sync=row.get_active())
            self.window.settings_changed()

    def _on_interval(self, *_):
        if not self._loading:
            backend.update_config(
                remote_refresh_interval_seconds=int(self.refresh_row.get_value()) * 60,
                upload_interval_seconds=int(self.upload_row.get_value()),
            )
            self.window.settings_changed()

    # storage --------------------------------------------------------------

    def _build_storage(self):
        group = Adw.PreferencesGroup(title="Storage")
        self.files_row = Adw.ActionRow(title="Downloaded Files")
        group.add(self.files_row)
        self.usage_row = Adw.ActionRow(title="Space Used", subtitle="Calculating…", use_markup=False)
        group.add(self.usage_row)
        clear = Adw.ButtonRow(title="Remove Downloaded Copies…")
        clear.connect("activated", lambda *_: self.window.clear_cache())
        group.add(clear)
        return group

    def _refresh_counts(self):
        counts = backend.file_counts()
        if counts is None:
            self.files_row.set_subtitle("Nothing downloaded yet")
        else:
            done, total = counts
            self.files_row.set_subtitle(f"{done:,} of {total:,}")

    def _refresh_storage(self):
        self._refresh_counts()
        cache = Path(backend.load_config()["cache_dir"]).expanduser()

        def worker():
            size = backend.disk_usage(cache)
            GLib.idle_add(self.usage_row.set_subtitle, f"{GLib.format_size(size)} in {cache}")

        threading.Thread(target=worker, daemon=True).start()


# ── log viewer ──────────────────────────────────────────────────────────────

class LogDialog(Adw.Dialog):
    def __init__(self):
        super().__init__(title="Log", content_width=900, content_height=600)
        self.buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buffer, editable=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                            left_margin=12, right_margin=12)
        self.scroller = Gtk.ScrolledWindow(child=view, vexpand=True)
        reload_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload")
        reload_button.connect("clicked", lambda *_: self.reload())
        header = Adw.HeaderBar()
        header.pack_start(reload_button)
        toolbar = Adw.ToolbarView(content=self.scroller)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.reload()

    def reload(self):
        self.buffer.set_text(backend.tail_log() or "No log yet.")
        GLib.idle_add(lambda: self.scroller.get_vadjustment().set_value(
            self.scroller.get_vadjustment().get_upper()))


# ── window ──────────────────────────────────────────────────────────────────

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="iCloud Drive", default_width=640, default_height=760)
        self.service = backend.Service()
        self.photos_service = backend.Service(backend.PHOTOS_UNIT)
        self.main_page = None
        self.nav = Adw.NavigationView()
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_content(self.toasts)

        action = Gio.SimpleAction.new("show-log", None)
        action.connect("activate", lambda *_: LogDialog().present(self))
        self.add_action(action)

        if backend.is_configured():
            self.show_main()
        else:
            self.nav.replace([welcome_page(self.start_setup)])

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=6))

    def show_main(self):
        if self.main_page is None:
            self.main_page = MainPage(self)
        else:
            self.main_page.load_settings()
            self.main_page.refresh()
        self.nav.replace([self.main_page])

    def settings_changed(self):
        if self.service.running and self.main_page:
            self.main_page.banner.set_revealed(True)

    def open_folder(self, path=None):
        folder = Gio.File.new_for_path(str(path or backend.mount_dir()))
        Gtk.FileLauncher.new(folder).launch(self, None, None)

    def enable_photos(self):
        def start(_error=None):
            self.photos_service.start(lambda error: error and self.toast(f"Could not start iCloud Photos: {error}"))

        if self.photos_service.installed:
            self.photos_service.set_enabled(True, start)
            return

        mount = backend.photos_mount_dir()

        def after_init(ok, output):
            if not ok:
                self.toast(output.strip().splitlines()[-1] if output.strip() else "Could not set up iCloud Photos.")
                self.main_page.refresh()
                return
            backend.add_bookmark(mount, "iCloud Photos")
            self.photos_service.daemon_reload(start)

        backend.run_icloudctl(["photos-init", str(mount)], after_init)

    # setup -----------------------------------------------------------------

    def start_setup(self):
        self.flow = SignInFlow(self)
        self.flow.connect("signed-in", self._on_setup_signed_in)
        self.nav.push(self.flow.page)

    def _on_setup_signed_in(self, _flow, username, password):
        self.credentials = (username, password)
        folder = FolderPage(self)
        folder.connect("finish", self._finish_setup)
        self.nav.push(folder)

    def _finish_setup(self, folder):
        username, password = self.credentials
        del self.credentials
        working, status = working_page("Setting Up…")
        status.set_description("Preparing the sync service. This can take a minute the first time.")
        self.nav.push(working)

        def fail(message):
            self.nav.pop_to_tag("folder")
            self.toast(message)

        try:
            backend.store_password(username, password)
        except GLib.Error as exc:
            fail(f"Could not save the password in GNOME Keyring: {exc.message}")
            return
        backend.update_config(username=username, password=None, warmup_mode=folder.warmup_mode)
        mount = folder.mount
        mount.mkdir(parents=True, exist_ok=True)

        def after_init(ok, output):
            if not ok:
                fail(f"Setup failed: {output.strip().splitlines()[-1] if output.strip() else 'icloudctl init'}")
                return
            if folder.bookmark.get_active():
                backend.add_bookmark(mount)
            self.service.daemon_reload(lambda _e: after_reload())

        def after_reload():
            def start(_error=None):
                self.service.start(self._report_start)
                self.show_main()

            if folder.autostart.get_active():
                start()
            else:
                self.service.set_enabled(False, start)

        backend.run_icloudctl(["init", str(mount)], after_init)

    def _report_start(self, error):
        if error:
            self.toast(f"Could not start iCloud Drive: {error}")

    # account ---------------------------------------------------------------

    def reauthenticate(self):
        username = backend.load_config().get("username")
        self.flow = SignInFlow(self, username=username)
        self.flow.connect("signed-in", self._on_reauthenticated)
        self.nav.push(self.flow.page)

    def _on_reauthenticated(self, _flow, username, password):
        try:
            backend.store_password(username, password)
        except GLib.Error as exc:
            self.toast(f"Could not save the password in GNOME Keyring: {exc.message}")
        backend.update_config(password=None)
        self.nav.pop_to_page(self.main_page)
        self.toast("Signed in.")
        self.service.restart(self._report_start)
        if self.photos_service.running:
            # photos.py copies the fresh session when it starts.
            self.photos_service.restart()

    def sign_out(self):
        pending = backend.read_status().get("pending_uploads") or 0
        body = ("iCloud Drive will stop syncing and the copies downloaded to this computer "
                "will be removed. Your files in iCloud are not affected.")
        if pending:
            body += "\n\n" + ngettext(
                "%d local change has not been uploaded yet and will be lost.",
                "%d local changes have not been uploaded yet and will be lost.", pending) % pending
        dialog = Adw.AlertDialog(heading="Sign Out of iCloud Drive?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("sign-out", "Sign Out")
        dialog.set_response_appearance("sign-out", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _d, r: r == "sign-out" and self._do_sign_out())
        dialog.present(self)

    def _do_sign_out(self):
        username = backend.load_config().get("username")
        mount = backend.mount_dir()
        photos_mount = backend.photos_mount_dir()
        working, _status = working_page("Signing Out…")
        self.nav.push(working)

        def done(ok, output):
            if username:
                try:
                    backend.clear_password(username)
                except GLib.Error:
                    pass
            backend.remove_bookmark(mount)
            backend.remove_bookmark(photos_mount)
            self.main_page = None
            self.nav.replace([welcome_page(self.start_setup)])
            if not ok:
                self.toast(output.strip().splitlines()[-1] if output.strip() else "Sign-out failed.")

        backend.run_icloudctl(["uninstall", "--purge"], done)

    def clear_cache(self):
        pending = backend.read_status().get("pending_uploads") or 0
        body = "Files will download again from iCloud as needed. Your files in iCloud are not affected."
        if pending:
            body += "\n\n" + ngettext(
                "%d local change has not been uploaded yet and will be lost.",
                "%d local changes have not been uploaded yet and will be lost.", pending) % pending
        dialog = Adw.AlertDialog(heading="Remove Downloaded Copies?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Remove")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_dialog, response):
            if response != "clear":
                return
            self.toast("Removing downloaded copies…")
            backend.run_icloudctl(["clear-cache"], lambda ok, out: (
                self.main_page._refresh_storage(),
                self.toast("Downloaded copies removed." if ok else out.strip() or "Failed."),
            ))

        dialog.connect("response", on_response)
        dialog.present(self)

    def move_mount(self, old, new):
        was_running = self.service.running
        was_enabled = self.service.enabled
        working, _status = working_page("Moving Folder…")
        self.nav.push(working)
        new.mkdir(parents=True, exist_ok=True)

        def after_stop(_error):
            backend.run_icloudctl(["init", str(new)], after_init)

        def after_init(ok, output):
            if not ok:
                self.nav.pop_to_page(self.main_page)
                self.toast(output.strip().splitlines()[-1] if output.strip() else "Could not move the folder.")
                return
            backend.remove_bookmark(old)
            backend.add_bookmark(new)
            self.service.daemon_reload(lambda _e: after_reload())

        def after_reload():
            def finish(_error=None):
                if was_running:
                    self.service.start(self._report_start)
                self.main_page.load_settings()
                self.nav.pop_to_page(self.main_page)

            if was_enabled:
                finish()
            else:
                self.service.set_enabled(False, finish)

        self.service.stop(after_stop)
