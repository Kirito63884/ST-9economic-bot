#!/usr/bin/env bash
# Compatibility entry point: one reviewed package preserves all current games/roles.
set -euo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Укажите полный SHA коммита первым аргументом."
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Запустите установщик от root."
    exit 1
fi
installer=$(mktemp /tmp/games-access-installer.XXXXXX)
trap 'rm -f "$installer"' EXIT
curl -fL --max-time 60 \
    "https://raw.githubusercontent.com/Kirito63884/ST-9economic-bot/$revision/scripts/update_games_and_access.sh" \
    -o "$installer"
bash "$installer" "$revision" "$app_dir"
