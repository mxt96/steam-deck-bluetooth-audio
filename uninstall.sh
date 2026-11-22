#!/usr/bin/env bash
set -euo pipefail

app=steam-deck-bluetooth-audio
unit="$app.service"
app_dir="$HOME/.local/share/$app"
bin="$HOME/.local/bin/$app"
unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
mark='# Managed by Steam Deck Bluetooth Audio Multicast'

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

(( EUID != 0 )) || die "Run this as the user who installed the application."
[[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]] ||
    die "Open Konsole in Desktop Mode and run the uninstaller again."
exec 9<"$XDG_RUNTIME_DIR"
flock -x 9

if [[ ! -f "$app_dir/.managed" ]]; then
    echo "No installation found."
    exit 0
fi
[[ ! -L "$app_dir" ]] || die "The installation directory has been replaced by a symbolic link."
for destination in "$bin" "$unit_file"; do
    if [[ -L "$destination" ]] ||
        { [[ -e "$destination" ]] && ! grep -Fxq -- "$mark" "$destination"; }; then
        die "The path $destination belongs to another application."
    fi
done

if [[ -f "$unit_file" ]]; then
    systemctl --user disable --now "$unit" ||
        die "Could not stop the service. Check the systemd user manager."
fi
if [[ -x "$app_dir/deck_audio.py" ]]; then
    "$app_dir/deck_audio.py" cleanup ||
        die "Could not check or clean up the audio stack. Run the uninstaller again in an active audio session."
fi
rm -f -- "$bin" "$unit_file" "$app_dir/deck_audio.py" "$app_dir/uninstall.sh" "$app_dir/.managed"
rmdir -- "$app_dir" 2>/dev/null || true
runtime_dir="$XDG_RUNTIME_DIR/$app"
if [[ -d "$runtime_dir" ]]; then
    rm -f -- "$runtime_dir/lock" "$runtime_dir/previous-sink.tmp"
    rmdir -- "$runtime_dir" 2>/dev/null || true
fi
systemctl --user daemon-reload || die "The files were removed. Reloading the systemd units failed."
echo "Steam Deck Bluetooth Audio Multicast was removed."
