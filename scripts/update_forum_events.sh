#!/usr/bin/env bash
# Install forum events while preserving installed shared features.
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
test -f "$app_dir/automation_admin.py"
test -f "$app_dir/discord_roster.py"
files=(
    discord_bot.py automation_admin.py templates/admin.html templates/_site_nav.html
    forum_events.py forum_events_admin.py discord_forum_events.py
    forum_event_extras.py forum_event_delivery.py forum_calendar.py
    templates/calendar.html templates/_calendar.js.html
    templates/_admin_forum_events.html templates/_admin_forum_events.js.html
)
services=(balance-app.service discord-bot.service)
stage=$(mktemp -d /tmp/forum-events.XXXXXX)
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
"$app_dir/venv/bin/python" "$stage/code/scripts/patch_forum_events.py" "$app_dir" "$stage/code"
python_files=()
for path in "${files[@]}"; do
    test -f "$stage/code/$path"
    if [[ "$path" == *.py ]]; then python_files+=("$stage/code/$path"); fi
    if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
"$app_dir/venv/bin/python" -m py_compile "${python_files[@]}"
backup="/root/forum-events-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
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
from jinja2 import meta
import main
import discord_bot
from forum_events import FORUM_ID
from discord_forum_events import register_forum_events
assert FORUM_ID == 1568007339343093951
assert discord_bot.bot.tree.get_command("event_create")
assert discord_bot.bot.tree.get_command("event_edit")
assert discord_bot.bot.tree.get_command("event_result")
assert discord_bot.bot.tree.get_command("event_from_template")
# Included routers need not expose .path; OpenAPI resolves nested routers.
paths = main.app.openapi()["paths"]
assert "get" in paths.get("/api/admin/forum-events", {})
assert "post" in paths.get("/api/admin/forum-events", {})
assert "get" in paths.get("/calendar", {})
assert "get" in paths.get("/api/calendar", {})
pending = ["admin.html", "calendar.html", "_admin_forum_events.html", "_admin_forum_events.js.html"]
seen = set()
while pending:
    name = pending.pop()
    if name in seen:
        continue
    seen.add(name)
    source = main.templates.env.loader.get_source(main.templates.env, name)[0]
    parsed = main.templates.env.parse(source)
    pending.extend(ref for ref in meta.find_referenced_templates(parsed) if ref is not None)
print("Маршруты событий, команды бота и шаблоны проверены.")
PY
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
echo "Готово. Ctrl+F5 → Админка → События; Discord: /event_create, /event_edit."
echo "Перезапущены сайт и Discord-бот. База не заменялась; сборщики не перезапускались."
