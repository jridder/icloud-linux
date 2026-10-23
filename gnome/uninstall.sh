#!/usr/bin/env bash
# Remove what install.sh added.
#   --purge  also delete config, session cookies, cache, logs and the keyring entry
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA_HOME/icloud-linux"
APP_ID="io.github.jridder.ICloudLinux"
UUID="icloud-linux@jridder.local"
CONFIG_FILE="$HOME/.config/icloud-linux/config.yaml"
purge="${1:-}"

username=""
if [[ -f "$CONFIG_FILE" ]]; then
  username="$(sed -n 's/^username: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$CONFIG_FILE" | head -1)"
fi

mount=""
if [[ -f "$HOME/.config/icloud-linux/icloud.env" ]]; then
  # shellcheck disable=SC1091
  mount="$(source "$HOME/.config/icloud-linux/icloud.env" && printf %s "${ICLOUD_MOUNT:-}")"
fi

photos_mount=""
if [[ -f "$HOME/.config/icloud-linux/photos.env" ]]; then
  # shellcheck disable=SC1091
  photos_mount="$(source "$HOME/.config/icloud-linux/photos.env" && printf %s "${ICLOUD_PHOTOS_MOUNT:-}")"
fi

if [[ -x "$DEST/icloudctl" ]]; then
  "$DEST/icloudctl" uninstall ${purge:+--purge}
else
  systemctl --user disable --now icloud.service >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/icloud.service"
  systemctl --user daemon-reload
fi

if [[ "$purge" == "--purge" && -n "$username" ]]; then
  secret-tool clear service icloud-linux username "$username" 2>/dev/null || true
fi

for m in "$mount" "$photos_mount"; do
  if [[ -n "$m" && -f "$HOME/.config/gtk-3.0/bookmarks" ]]; then
    uri="$(python3 -c 'import sys; from gi.repository import Gio; print(Gio.File.new_for_path(sys.argv[1]).get_uri())' "$m")"
    sed -i "\\|^$uri\\( \\|\$\\)|d" "$HOME/.config/gtk-3.0/bookmarks"
  fi
done

gnome-extensions disable "$UUID" 2>/dev/null || true
rm -rf "$DATA_HOME/gnome-shell/extensions/$UUID" "$DEST"
rm -f "$HOME/.local/bin/icloud-drive" "$DATA_HOME/applications/$APP_ID.desktop"
rm -f "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg" "$DATA_HOME"/icons/hicolor/symbolic/apps/"$APP_ID"-*symbolic.svg
gtk-update-icon-cache -q -t -f "$DATA_HOME/icons/hicolor" 2>/dev/null || true

echo "Removed iCloud Drive."
[[ "$purge" == "--purge" ]] || echo "Config, cookies and cache were kept; run with --purge to remove them."
