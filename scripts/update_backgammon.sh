#!/usr/bin/env bash
# Explicit owner-run deployment only; source revision must include all merged games.
set -euo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Укажите полный SHA согласованного коммита из main."
    exit 1
fi
installer=$(mktemp /tmp/backgammon-install.XXXXXX)
trap 'rm -f "$installer"' EXIT
curl -fL --max-time 60 \
    "https://raw.githubusercontent.com/Kirito63884/ST-9economic-bot/$revision/scripts/update_games_and_access.sh" \
    -o "$installer"
bash "$installer" "$revision" "$app_dir"
echo "Нарды установлены: Ctrl+F5 → Игры → Нарды → Длинные / Короткие."
echo "Перед началом оба подтверждают равный взнос. Весь банк получает победитель."
