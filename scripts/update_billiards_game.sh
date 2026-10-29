#!/usr/bin/env bash
# Pool lounge, canvas renderer and authoritative 8-ball rules; web service only.
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
test -x "$app_dir/venv/bin/python"
test -f "$app_dir/main.py"
test -f "$app_dir/billiards.py"
files=(
    billiards.py billiards_routes.py
    templates/billiards_lobby.html templates/billiards_table.html templates/_billiards_ui.html
    templates/_billiards_renderer.js.html templates/_billiards_game.js.html
)
services=(balance-app.service)
stage=$(mktemp -d /tmp/billiards-game.XXXXXX)
backup=""
changing=0
original=()
absent=()
cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ "$changing" == 1 ]]; then
        echo "Ошибка обновления. Восстанавливаем прежние файлы."
        systemctl stop "${services[@]}" || true
        tar -xzf "$backup/files.tar.gz" -C "$app_dir" || return 1
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start "${services[@]}" || true
        echo "Код восстановлен. Рабочая база не заменялась."
    fi
    echo "Обновление не завершено. Пришлите вывод терминала."
    exit 1
}
trap cleanup EXIT
trap rollback ERR
curl -fL --max-time 120 \
    "https://github.com/Kirito63884/ST-9economic-bot/archive/$revision.tar.gz" \
    -o "$stage/update.tar.gz"
mkdir "$stage/code"
tar -xzf "$stage/update.tar.gz" -C "$stage/code" --strip-components=1
python_files=()
for path in "${files[@]}"; do
    test -f "$stage/code/$path"
    if [[ "$path" == *.py ]]; then python_files+=("$stage/code/$path"); fi
    if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
"$app_dir/venv/bin/python" -m py_compile "${python_files[@]}"
backup="/root/billiards-game-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
mkdir -m 700 "$backup"
tar -czf "$backup/files.tar.gz" -C "$app_dir" "${original[@]}"
tar -tzf "$backup/files.tar.gz" >/dev/null
# SQLite's backup API captures a consistent database even while other workers run.
(
    cd "$app_dir"
    ./venv/bin/python - "$backup/database.sqlite3" <<'PY'
import sqlite3
import sys
from database import engine
if engine.dialect.name != "sqlite":
    raise SystemExit("Для этого установщика требуется SQLite. Код ещё не изменён.")
source = engine.raw_connection()
try:
    with sqlite3.connect(sys.argv[1]) as target:
        source.driver_connection.backup(target)
        if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SystemExit("Резервная копия базы не прошла проверку")
finally:
    source.close()
print("Проверенная резервная копия базы готова.")
PY
)
echo "Резервная копия: $backup"
changing=1
systemctl stop "${services[@]}"
for path in "${files[@]}"; do
    install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"
done
(
    cd "$app_dir"
    runuser -u www-data -- ./venv/bin/python - <<'PY'
from billiards import BilliardsTable
from billiards_routes import BilliardsService
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from database import engine, AppRuntime
from sqlalchemy.orm import Session
import json
# Validate durable tables before changing the served page. Reading does not
# settle games, change balances or call external services.
with Session(engine) as db:
    row = db.get(AppRuntime, 1)
    state = json.loads(row.payload or "{}") if row else {}
    for saved in state.get("billiards", {}).values():
        BilliardsTable.from_dict(saved)
env = Environment(loader=FileSystemLoader("templates"))
for name in ("billiards_lobby.html", "billiards_table.html", "_billiards_ui.html",
             "_billiards_renderer.js.html", "_billiards_game.js.html"):
    env.parse(Path("templates", name).read_text())
print("Сохранённые столы и игровые шаблоны проверены; схема базы не менялась.")
PY
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
echo "Готово. Ctrl+F5 → Игры → Бильярд."
echo "Обновлён сайт. Discord-бот, SQSTAT и игровой монитор не перезапускались."
echo "Сохранённые партии продолжаются. Новые правила 8-ball включаются со следующей партией."
