#!/usr/bin/env bash
set -euo pipefail

app=steam-deck-bluetooth-audio
unit="$app.service"
app_dir="$HOME/.local/share/$app"
bin="$HOME/.local/bin/$app"
unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
mark='# Managed by Steam Deck Bluetooth Audio Multicast'

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

(( EUID != 0 )) || die "Запустите от пользователя, который установил программу."
[[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]] ||
    die "Откройте Konsole в Desktop Mode и повторите удаление."
exec 9<"$XDG_RUNTIME_DIR"
flock -x 9

if [[ ! -f "$app_dir/.managed" ]]; then
    echo "Установка не найдена."
    exit 0
fi
[[ ! -L "$app_dir" ]] || die "Каталог установки заменён символьной ссылкой."
for destination in "$bin" "$unit_file"; do
    if [[ -L "$destination" ]] ||
        { [[ -e "$destination" ]] && ! grep -Fxq -- "$mark" "$destination"; }; then
        die "Путь $destination принадлежит другой программе."
    fi
done

if [[ -f "$unit_file" ]]; then
    systemctl --user disable --now "$unit" ||
        die "Не удалось остановить сервис. Проверьте пользовательский systemd."
fi
if [[ -x "$app_dir/deck_audio.py" ]]; then
    "$app_dir/deck_audio.py" cleanup ||
        die "Не удалось проверить или очистить аудиостек. Повторите удаление в работающей аудиосессии."
fi
rm -f -- "$bin" "$unit_file" "$app_dir/deck_audio.py" "$app_dir/uninstall.sh" "$app_dir/.managed"
rmdir -- "$app_dir" 2>/dev/null || true
runtime_dir="$XDG_RUNTIME_DIR/$app"
if [[ -d "$runtime_dir" ]]; then
    rm -f -- "$runtime_dir/lock" "$runtime_dir/previous-sink.tmp"
    rmdir -- "$runtime_dir" 2>/dev/null || true
fi
systemctl --user daemon-reload || die "Файлы удалены. Не удалось перечитать юниты systemd."
echo "Steam Deck Bluetooth Audio Multicast удалён."
