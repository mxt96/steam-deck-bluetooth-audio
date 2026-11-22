#!/usr/bin/env bash
set -euo pipefail

app=steam-deck-bluetooth-audio
unit="$app.service"
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
app_dir="$HOME/.local/share/$app"
bin="$HOME/.local/bin/$app"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_file="$unit_dir/$unit"
mark='# Managed by Steam Deck Bluetooth Audio Multicast'

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "Linux with a SteamOS-compatible audio stack is required."
(( EUID != 0 )) || die "Run this as your regular Steam Deck user."
[[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]] ||
    die "Open Konsole in Desktop Mode and run this in your user session."
if ! awk -F= '$1 == "ID" && ($2 == "steamos" || $2 == "\"steamos\"") {found=1} END {exit !found}' /etc/os-release; then
    printf '%s\n' 'This system is running Linux outside SteamOS. Installation will continue if the audio stack is compatible.' >&2
fi
for dependency in python3 pactl wpctl systemctl flock; do
    command -v "$dependency" >/dev/null || die "Required command $dependency is unavailable."
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is required."
[[ -f "$root/src/deck_audio.py" && -f "$root/src/$app" && -f "$root/systemd/$unit" && -f "$root/uninstall.sh" ]] ||
    die "Download the full archive or clone the repository before running this script."
systemctl --user show-environment >/dev/null || die "The systemd user manager is unavailable."
python3 "$root/src/deck_audio.py" check ||
    die "Run this in a SteamOS audio session with PipeWire, pipewire-pulse, and WirePlumber."

exec 9<"$XDG_RUNTIME_DIR"
flock -x 9
if [[ -L "$app_dir" || ( -e "$app_dir" && ! -f "$app_dir/.managed" ) ]]; then
    die "The directory $app_dir is already in use."
fi
for destination in "$bin" "$unit_file"; do
    if [[ -L "$destination" ]] ||
        { [[ -e "$destination" ]] && ! grep -Fxq -- "$mark" "$destination"; }; then
        die "The path $destination is already in use."
    fi
done

mkdir -p -- "$app_dir" "$(dirname -- "$bin")" "$unit_dir"
touch -- "$app_dir/.managed"

copy_file() {
    local source=$1 destination=$2 mode=$3 temporary
    temporary=$(mktemp -- "${destination}.XXXXXX")
    if ! install -m "$mode" -- "$source" "$temporary" || ! mv -fT -- "$temporary" "$destination"; then
        rm -f -- "$temporary"
        die "Could not install $destination."
    fi
}

copy_file "$root/src/deck_audio.py" "$app_dir/deck_audio.py" 0755
copy_file "$root/uninstall.sh" "$app_dir/uninstall.sh" 0755
copy_file "$root/src/$app" "$bin" 0755
copy_file "$root/systemd/$unit" "$unit_file" 0644
systemctl --user daemon-reload || die "Could not reload the systemd units."
systemctl --user enable "$unit" || die "Could not enable automatic startup."
systemctl --user restart "$unit" ||
    die "Could not start the service. Check the logs with journalctl --user -u $unit."
printf 'Installation complete. Check the service with %s status.\n' "$bin"
