import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk

from . import backend
from .window import Window

APP_ID = "io.github.jridder.ICloudLinux"
VERSION = "0.1.0"


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        for name, callback, accels in (
            ("about", self._about, []),
            ("quit", lambda *_: self.quit(), ["<primary>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<primary>w"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Running from a checkout: icons aren't installed in hicolor yet.
        icons = backend.BACKEND_DIR / "gnome" / "data" / "icons"
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons))
        Gtk.Window.set_default_icon_name(APP_ID)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="iCloud Drive",
            application_icon=APP_ID,
            version=VERSION,
            comments="GNOME front end for icloud-linux, a FUSE client for iCloud Drive.",
            website="https://github.com/IsmaeelAkram/icloud-linux",
            license_type=Gtk.License.UNKNOWN,
            developers=["Ismaeel Akram (icloud-linux)", "Jeff (GNOME app)"],
        )
        about.present(self.props.active_window)


def main():
    return Application().run(sys.argv)
