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

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "Требуется Linux со стеком SteamOS."
(( EUID != 0 )) || die "Запустите от обычного пользователя Steam Deck."
[[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]] ||
    die "Откройте Konsole в Desktop Mode и выполните команду в пользовательской сессии."
if ! awk -F= '$1 == "ID" && ($2 == "steamos" || $2 == "\"steamos\"") {found=1} END {exit !found}' /etc/os-release; then
    printf '%s\n' 'Обнаружен Linux вне SteamOS. Установка продолжится при наличии совместимого аудиостека.' >&2
fi
for dependency in python3 pactl wpctl systemctl flock; do
    command -v "$dependency" >/dev/null || die "Команда $dependency недоступна."
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Нужен Python 3.10 или новее."
[[ -f "$root/src/deck_audio.py" && -f "$root/src/$app" && -f "$root/systemd/$unit" && -f "$root/uninstall.sh" ]] ||
    die "Нужен полный архив или клон репозитория."
systemctl --user show-environment >/dev/null || die "Пользовательский systemd недоступен."
python3 "$root/src/deck_audio.py" check ||
    die "Откройте аудиосессию SteamOS с PipeWire, pipewire-pulse и WirePlumber."

exec 9<"$XDG_RUNTIME_DIR"
flock -x 9
if [[ -L "$app_dir" || ( -e "$app_dir" && ! -f "$app_dir/.managed" ) ]]; then
    die "Каталог $app_dir уже используется."
fi
for destination in "$bin" "$unit_file"; do
    if [[ -L "$destination" ]] ||
        { [[ -e "$destination" ]] && ! grep -Fxq -- "$mark" "$destination"; }; then
        die "Путь $destination уже используется."
    fi
done

mkdir -p -- "$app_dir" "$(dirname -- "$bin")" "$unit_dir"
touch -- "$app_dir/.managed"

copy_file() {
    local source=$1 destination=$2 mode=$3 temporary
    temporary=$(mktemp -- "${destination}.XXXXXX")
    if ! install -m "$mode" -- "$source" "$temporary" || ! mv -fT -- "$temporary" "$destination"; then
        rm -f -- "$temporary"
        die "Не удалось установить $destination."
    fi
}

copy_file "$root/src/deck_audio.py" "$app_dir/deck_audio.py" 0755
copy_file "$root/uninstall.sh" "$app_dir/uninstall.sh" 0755
copy_file "$root/src/$app" "$bin" 0755
copy_file "$root/systemd/$unit" "$unit_file" 0644
systemctl --user daemon-reload || die "Не удалось перечитать юниты systemd."
systemctl --user enable "$unit" || die "Не удалось включить автозапуск."
systemctl --user restart "$unit" ||
    die "Не удалось запустить сервис. См. journalctl --user -u $unit."
printf 'Готово. Состояние можно проверить командой %s status\n' "$bin"
