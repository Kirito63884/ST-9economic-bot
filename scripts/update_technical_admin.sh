#!/usr/bin/env bash
# Owner-only technical administrator delegation; additive migration and source rollback.
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
test -f "$app_dir/backup_routes.py"
files=(
    main.py database.py backup_routes.py roster_membership.py
    templates/admin.html
)
services=(balance-app.service)
stage=$(mktemp -d /tmp/technical-admin.XXXXXX)
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
# The bot imports the user model and roster code; it must load the new fields too.
services+=(discord-bot.service)
backup="/root/technical-admin-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
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
from database import User, engine, run_migrations
from sqlalchemy import inspect
run_migrations()
assert {"is_tech_admin", "auth_version"} <= {column["name"] for column in inspect(engine).get_columns("users")}
import main  # Import preflight only; no background integrations are started.
assert hasattr(main, "has_owner_access")
print("Поля технического администратора добавлены. Ни одному игроку права автоматически не выданы.")
PY
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
echo "Готово. Ctrl+F5 → Админка → Игроки → карточка игрока → Права доступа."
echo "Назначение и снятие техадмина доступны только владельцу. После назначения игрок входит заново."
echo "Перезапущены: ${services[*]}. Сборщики SQSTAT и игрового онлайна не перезапускались."
