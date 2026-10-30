#!/usr/bin/env bash
# Two-player sea battle; safe source update, private SQLite backup and rollback.
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
test -f "$app_dir/wallet_history.py"
files=(
    main.py assembly_attendance.py assembly_waivers.py groups_admin.py assembly_reports.py
    medals_routes.py protocol_rankings.py
    templates/admin.html templates/_assembly_waiver.html templates/_admin_group_reports.html
    templates/_admin_audit.html templates/_admin_hub.html templates/_statistics_freshness.html
    templates/medals.html templates/_admin_medals.html templates/_award_design.html
    templates/_medals_style.html templates/_award_catalog.html templates/_award_gallery_style.html
    templates/_admin_award_catalog.html templates/_protocol_rankings.html
    billiards.py billiards_routes.py durak.py durak_routes.py table_admin.py table_lifecycle.py
    templates/billiards_lobby.html templates/billiards_table.html templates/_billiards_ui.html
    templates/_billiards_renderer.js.html templates/_billiards_game.js.html templates/_table_admin_ui.html
    domino.py domino_routes.py home_activity.py audit_logs.py
    templates/domino_lobby.html templates/domino_table.html templates/_domino_ui.html
    templates/_domino_rules.html templates/_domino_tiles.js.html templates/_domino_game.js.html
    templates/games.html templates/_site_nav.html templates/profile.html templates/index.html
    templates/_admin_tables.html
    battleship.py battleship_routes.py
    templates/battleship_lobby.html templates/battleship_table.html
    templates/_battleship_ui.html templates/_battleship_rules.html
    templates/_battleship_shared.js.html templates/_battleship_game.js.html
    templates/_battleship_ships.js.html templates/_battleship_input.js.html
)
services=(balance-app.service)
stage=$(mktemp -d /tmp/battleship-game.XXXXXX)
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
# A merged source archive may also refer to another developer's new games. Never
# install main.py without their modules: report missing prerequisites before stop.
"$app_dir/venv/bin/python" - "$stage/code" "$app_dir" "${files[@]}" <<'PY'
import ast
from pathlib import Path
import sys
stage, target = map(Path, sys.argv[1:3])
installed = set(sys.argv[3:])
seen, pending, missing = set(), ["main"], []
while pending:
    name = pending.pop()
    if name in seen:
        continue
    seen.add(name)
    source = stage / (name + ".py")
    if not source.is_file():
        continue
    if name + ".py" not in installed and not (target / (name + ".py")).is_file():
        missing.append(name + ".py")
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import):
            pending.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            pending.append(node.module.split(".")[0])
if missing:
    raise SystemExit("Сначала установите зависимости этого коммита: " + ", ".join(sorted(missing)))
PY
# Restart the bot only if the previous assembly-policy update is still missing.
for path in assembly_attendance.py assembly_reports.py assembly_waivers.py; do
    if ! cmp -s "$stage/code/$path" "$app_dir/$path"; then
        services+=(discord-bot.service)
        break
    fi
done
backup="/root/battleship-game-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
mkdir -m 700 "$backup"
tar -czf "$backup/files.tar.gz" -C "$app_dir" "${original[@]}"
tar -tzf "$backup/files.tar.gz" >/dev/null
# SQLite's backup API captures a consistent database even while other workers run.
(
    cd "$app_dir"
    ./venv/bin/python - "$backup/database.sqlite3" "$stage/code/main.py" <<'PY'
import ast
import json
from pathlib import Path
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
        row = target.execute("SELECT payload FROM app_runtime WHERE id=1").fetchone()
        snapshot = json.loads(row[0]) if row else {}
        tree = ast.parse(Path(sys.argv[2]).read_text())
        serializer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "serialize_runtime")
        supported = {key.value for node in ast.walk(serializer) if isinstance(node, ast.Dict) for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        unknown = set(snapshot) - supported
        if unknown:
            raise SystemExit("Этот коммит не сохраняет уже установленные игры/данные: " + ", ".join(sorted(unknown)) + ". Выберите согласованный коммит после объединения всех нужных PR.")
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
from database import engine
from sqlalchemy import inspect
required = {"automation_settings", "bulk_confirmations", "assembly_assessments", "assembly_windows", "assembly_appeals"}
if not required <= set(inspect(engine).get_table_names()):
    raise SystemExit("Не найдены таблицы сборов и подтверждений. Требуется предыдущее обновление.")
from assembly_waivers import PATH
from domino import DominoTable
from domino_routes import DominoService
from battleship import BattleshipTable
from battleship_routes import BattleshipService
import main  # Verify all entry-point imports without starting background workers.
from table_lifecycle import IDLE_SECONDS
from table_admin import management
from protocol_rankings import CATEGORIES
from medals_routes import catalog_context
print("Компоненты морского боя и прежних игр доступны; новые таблицы морскому бою не нужны.")
PY
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
echo "Готово. Ctrl+F5 → Игры → Морской бой."
echo "Классический морской бой на двоих. Равные взносы, победителю весь банк без комиссии."
echo "Расставьте корабли, подтвердите готовность и запустите бой. Стреляйте кликом или коротким касанием клетки соперника."
echo "Перезапущены: ${services[*]}. Сборщики SQSTAT и игрового онлайна не перезапускались."
