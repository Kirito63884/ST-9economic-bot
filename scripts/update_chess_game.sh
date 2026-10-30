#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then echo "Укажите полный SHA коммита первым аргументом."; exit 1; fi
if [[ $EUID -ne 0 ]]; then echo "Запустите установщик от root."; exit 1; fi
python="$app_dir/venv/bin/python"
test -x "$python"; test -f "$app_dir/main.py"
stage=$(mktemp -d /tmp/chess-game.XXXXXX)
backup=""
changing=0
absent=()
cleanup() { rm -rf "$stage"; }
rollback() {
  status=$?
  trap - ERR
  if [[ "$changing" == 1 && -n "$backup" ]]; then
    echo "Ошибка обновления. Восстанавливаем прежние файлы."
    systemctl stop balance-app.service || true
    tar -xzf "$backup/files.tar.gz" -C "$app_dir" || return 1
    for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
    systemctl start balance-app.service || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap rollback ERR
curl -fL --max-time 120 "https://github.com/Kirito63884/ST-9economic-bot/archive/$revision.tar.gz" -o "$stage/update.tar.gz"
mkdir "$stage/code"
tar -xzf "$stage/update.tar.gz" -C "$stage/code" --strip-components=1
files=(main.py chess_game.py chess_routes.py table_lifecycle.py table_admin.py home_activity.py audit_logs.py requirements.txt CHESS_GAME_UPDATE.md templates/games.html templates/index.html templates/profile.html templates/admin.html templates/admin_logs.html templates/_site_nav.html templates/_admin_audit.html templates/_admin_tables.html templates/chess_lobby.html templates/chess_table.html templates/_chess_ui.html templates/_chess_lobby.js.html templates/_chess_table.js.html scripts/update_chess_game.sh)
for path in "${files[@]}"; do test -f "$stage/code/$path"; done
"$python" -m pip install --disable-pip-version-check chess==1.11.2
"$python" -m py_compile "$stage/code/main.py" "$stage/code/chess_game.py" "$stage/code/chess_routes.py" "$stage/code/table_lifecycle.py" "$stage/code/table_admin.py" "$stage/code/home_activity.py" "$stage/code/audit_logs.py"
backup="/root/chess-game-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
mkdir -m 700 "$backup"
original=()
for path in "${files[@]}"; do
  if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
tar -czf "$backup/files.tar.gz" -C "$app_dir" "${original[@]}"
tar -tzf "$backup/files.tar.gz" >/dev/null
changing=1
systemctl stop balance-app.service
for path in "${files[@]}"; do install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"; done
(
  cd "$app_dir"
  runuser -u www-data -- "$python" - <<'PY'
import chess
from chess_game import ChessTable
from chess_routes import ChessService
from table_lifecycle import IDLE_SECONDS
assert chess.__version__ == "1.11.2"
print("Шахматы загружены; срок стола:", IDLE_SECONDS)
PY
)
systemctl start balance-app.service
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/ -o /dev/null
systemctl is-active balance-app.service
changing=0
echo "Установка завершена. Обновите страницу → Игры → Шахматы."
