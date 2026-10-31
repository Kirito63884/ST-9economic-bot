#!/usr/bin/env bash
# Install an exact reviewed revision using the shared verified updater.
set -euo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Укажите полный SHA объединённого коммита первым аргументом."
    exit 1
fi
installer=$(mktemp /tmp/monopoly-update.XXXXXX)
trap 'rm -f "$installer"' EXIT
curl -fL --max-time 60 \
    "https://raw.githubusercontent.com/Kirito63884/ST-9economic-bot/$revision/scripts/update_games_and_access.sh" \
    -o "$installer"
bash "$installer" "$revision" "$app_dir"
