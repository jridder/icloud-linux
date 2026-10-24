#!/usr/bin/env bash
# Install icloud-linux plus its GNOME app and panel extension for this user.
#
#   backend + app   ~/.local/share/icloud-linux/   (driver, icloudctl, venv, app)
#   launcher        ~/.local/bin/icloud-drive
#   desktop entry   ~/.local/share/applications/io.github.jridder.ICloudLinux.desktop
#   icons           ~/.local/share/icons/hicolor/
#   extension       ~/.local/share/gnome-shell/extensions/icloud-linux@jridder.local/
#
# Re-run to update; config, session cookies and cache are left alone.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA_HOME/icloud-linux"
BIN_DIR="$HOME/.local/bin"
APP_ID="io.github.jridder.ICloudLinux"
UUID="icloud-linux@jridder.local"
EXT_DIR="$DATA_HOME/gnome-shell/extensions/$UUID"
ICONS="$DATA_HOME/icons/hicolor"

missing=()
for cmd in python3 fusermount secret-tool systemctl gcc pkg-config; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
pkg-config --exists fuse 2>/dev/null || missing+=("fuse-devel")
python3 - <<'EOF' 2>/dev/null || missing+=("python3-gobject/libadwaita/libsecret/python3-pyyaml")
import gi, yaml
gi.require_version("Adw", "1"); gi.require_version("Secret", "1")
from gi.repository import Adw, Secret
EOF
if (( ${#missing[@]} )); then
  echo "Missing: ${missing[*]}"
  echo "On Fedora: sudo dnf install python3-devel fuse fuse-libs fuse-devel gcc make libsecret python3-gobject libadwaita python3-pyyaml"
  exit 1
fi

echo "Installing backend and app to $DEST"
mkdir -p "$DEST/gnome"
for f in driver.py photos.py auth.py hydrate.py hydrate_dir.py sync.py fix_filenames.py icloudctl \
         requirements.txt config.example.yaml README.md; do
  install -m "$( [[ -x "$SRC/$f" ]] && echo 755 || echo 644 )" "$SRC/$f" "$DEST/$f"
done
install -m 644 "$SRC/gnome/auth_helper.py" "$DEST/gnome/auth_helper.py"
rm -rf "$DEST/gnome/icloud_gnome"
mkdir -p "$DEST/gnome/icloud_gnome"
install -m 644 "$SRC"/gnome/icloud_gnome/*.py "$DEST/gnome/icloud_gnome/"

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
  echo "Creating virtualenv (builds fuse-python against libfuse)…"
  python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/icloud-drive" <<EOF
#!/usr/bin/python3
import sys
sys.path.insert(0, "$DEST/gnome")
from icloud_gnome.main import main
sys.exit(main())
EOF
chmod 755 "$BIN_DIR/icloud-drive"

mkdir -p "$DATA_HOME/applications"
cat > "$DATA_HOME/applications/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=iCloud Drive
Comment=Sync your iCloud Drive files
Exec=$BIN_DIR/icloud-drive
Icon=$APP_ID
Categories=Network;FileTransfer;GTK;
Keywords=icloud;apple;cloud;sync;drive;
StartupNotify=true
DBusActivatable=false
EOF

mkdir -p "$ICONS/scalable/apps" "$ICONS/symbolic/apps"
install -m 644 "$SRC/gnome/data/icons/$APP_ID.svg" "$ICONS/scalable/apps/"
install -m 644 "$SRC"/gnome/data/icons/*-symbolic.svg "$ICONS/symbolic/apps/"
gtk-update-icon-cache -q -t -f "$ICONS" 2>/dev/null || true
update-desktop-database -q "$DATA_HOME/applications" 2>/dev/null || true

echo "Installing GNOME Shell extension $UUID"
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR/icons"
install -m 644 "$SRC/gnome/extension/$UUID"/{metadata.json,extension.js,stylesheet.css} "$EXT_DIR/"
install -m 644 "$SRC"/gnome/data/icons/*-symbolic.svg "$SRC"/gnome/data/icons/sync-frames/*-symbolic.svg "$EXT_DIR/icons/"
if ! gnome-extensions enable "$UUID" 2>/dev/null; then
  # The shell only scans for new extensions at login; pre-enable it for then.
  current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")"
  if [[ "$current" != *"'$UUID'"* ]]; then
    if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
      gsettings set org.gnome.shell enabled-extensions "['$UUID']"
    else
      gsettings set org.gnome.shell enabled-extensions "${current%]}, '$UUID']"
    fi
  fi
  echo "  The panel icon appears after you log out and back in."
fi

# regenerate UNIT INIT_CMD FOLDER: rewrite an existing unit for this install.
# Folders still mounted on directly (before mounts moved behind links) are
# converted, which needs the service stopped for a moment.
regenerate() {
  local unit="$1" init_cmd="$2" folder="$3" was_enabled was_active=no
  was_enabled=$(systemctl --user is-enabled "$unit" 2>/dev/null || true)
  if [[ ! -L "$folder" ]]; then
    if systemctl --user is-active --quiet "$unit"; then
      was_active=yes
      echo "Moving $folder behind a link (restarting $unit)…"
      systemctl --user stop "$unit"
    fi
    fusermount -uz "$folder" >/dev/null 2>&1 || true
  fi
  "$DEST/icloudctl" "$init_cmd" "$folder" >/dev/null
  [[ "$was_enabled" == enabled ]] || systemctl --user disable "$unit" >/dev/null 2>&1 || true
  if [[ "$was_active" == yes ]]; then
    systemctl --user start "$unit"
  else
    echo "Updated $unit (restart it to pick up the new driver)."
  fi
}

if [[ -f "$HOME/.config/systemd/user/icloud.service" && -f "$HOME/.config/icloud-linux/icloud.env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.config/icloud-linux/icloud.env"
  regenerate icloud.service init "${ICLOUD_MOUNT:-$HOME/iCloud}"
fi
if [[ -f "$HOME/.config/systemd/user/icloud-photos.service" && -f "$HOME/.config/icloud-linux/photos.env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.config/icloud-linux/photos.env"
  regenerate icloud-photos.service photos-init "${ICLOUD_PHOTOS_MOUNT:-$HOME/iCloud Photos}"
fi

echo "Done. Open “iCloud Drive” from the app grid, or run: $BIN_DIR/icloud-drive"
