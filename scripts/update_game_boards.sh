#!/usr/bin/env bash
# Five game boards and animations; web service only, no dependency/schema changes.
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
test -f "$app_dir/chess_routes.py"
test -f "$app_dir/battleship_routes.py"
files=(
    chess_game.py battleship.py battleship_routes.py domino.py domino_routes.py
    templates/chess_table.html templates/_chess_ui.html templates/_chess_table.js.html
    templates/_battleship_game.js.html templates/_battleship_ships.js.html templates/_battleship_rules.html
    templates/_billiards_game.js.html
    templates/domino_table.html templates/_domino_game.js.html
    templates/_domino_tiles.js.html templates/_domino_ui.html templates/_domino_rules.html
    templates/poker_multiplayer.html
)
services=(balance-app.service)
stage=$(mktemp -d /tmp/game-boards.XXXXXX)
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
backup="/root/game-boards-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
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
from battleship import BattleshipTable
from chess_game import ChessTable
from domino import DominoTable
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from database import engine, AppRuntime
from sqlalchemy.orm import Session
import json
# Read and validate only: no workers, wallet settlement or runtime replacement.
with Session(engine) as db:
    row = db.get(AppRuntime, 1)
    state = json.loads(row.payload or "{}") if row else {}
    for key, model in (("billiards", BilliardsTable), ("battleship", BattleshipTable),
                       ("chess", ChessTable), ("domino", DominoTable)):
        for saved in state.get(key, {}).values():
            model.from_dict(saved)
env = Environment(loader=FileSystemLoader("templates"))
for name in ("chess_table.html", "_chess_ui.html", "_chess_table.js.html",
             "_battleship_game.js.html", "_battleship_rules.html", "_billiards_game.js.html",
             "domino_table.html", "_domino_game.js.html", "_domino_tiles.js.html",
             "_domino_ui.html", "_domino_rules.html", "poker_multiplayer.html"):
    env.parse(Path("templates", name).read_text())
print("Сохранённые партии и шаблоны проверены. База и зависимости не менялись.")
PY
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
echo "Готово. Ctrl+F5 → Шахматы / Домино / Морской бой / Бильярд / Покер."
echo "Перезапущен только сайт. Discord-бот и сборщики не перезапускались."
echo "После завершения морского боя участникам теперь видна расстановка соперника."
