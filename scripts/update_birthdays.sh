#!/usr/bin/env bash
# Install only the birthday module and a small bot registration patch.
set -euo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
import_file=${3:-}
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'Укажите полный SHA коммита.'; exit 1; }
[[ $EUID == 0 ]] || { echo 'Запустите от root.'; exit 1; }
test -x "$app_dir/venv/bin/python"
test -f "$app_dir/discord_bot.py"
if [[ -n "$import_file" ]]; then import_file=$(realpath "$import_file"); test -f "$import_file"; fi
stage=$(mktemp -d /tmp/st9-birthdays.XXXXXX)
backup=""
changing=0
files=(discord_bot.py discord_birthdays.py scripts/import_birthdays.py)
original=()
absent=()
cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ "$changing" == 1 ]]; then
        systemctl stop discord-bot.service || true
        tar -xzf "$backup/files.tar.gz" -C "$app_dir"
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start discord-bot.service || true
    fi
    echo 'Установка не завершена. Файлы восстановлены; рабочая БД не заменялась.'
    exit 1
}
trap cleanup EXIT
trap rollback ERR
curl -fL --max-time 120 "https://github.com/Kirito63884/ST-9economic-bot/archive/$revision.tar.gz" -o "$stage/code.tar.gz"
mkdir "$stage/code"
tar -xzf "$stage/code.tar.gz" -C "$stage/code" --strip-components=1
# Patch the installed bot, preserving unrelated updates.
"$app_dir/venv/bin/python" - "$app_dir/discord_bot.py" "$stage/code/discord_bot.py" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
block = 'from discord_birthdays import register_birthdays\n\nregister_birthdays(bot)\n'
anchor = '# ST9_SQUAD_BOT_END'
if block not in source:
    if source.count(anchor) != 1 or 'register_birthdays' in source:
        raise SystemExit('Неизвестная структура discord_bot.py; файлы не изменены.')
    source = source.replace(anchor, block + anchor)
Path(sys.argv[2]).write_text(source)
PY
for path in "${files[@]}"; do
    test -f "$stage/code/$path"
    "$app_dir/venv/bin/python" -m py_compile "$stage/code/$path"
    if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
# Validate private import before touching the installed code; no real Discord calls.
if [[ -n "$import_file" ]]; then
    DATABASE_URL="sqlite:///$stage/validation.sqlite3" PYTHON_DOTENV_DISABLED=1 PYTHONPATH="$stage/code" "$app_dir/venv/bin/python" - "$import_file" <<'PY'
import json, sys
from pathlib import Path
from discord_birthdays import validate_registry
print('Проверено записей:', len(validate_registry(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')))))
PY
fi
backup="/root/birthdays-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
mkdir -m 700 "$backup"
tar -czf "$backup/files.tar.gz" -C "$app_dir" "${original[@]}"
tar -tzf "$backup/files.tar.gz" >/dev/null
(
    cd "$app_dir"
    ./venv/bin/python - "$backup/database.sqlite3" <<'PY'
import sqlite3, sys
from database import engine
if engine.dialect.name != 'sqlite':
    raise SystemExit('Этот установщик поддерживает SQLite.')
source = engine.raw_connection()
try:
    with sqlite3.connect(sys.argv[1]) as dest:
        source.driver_connection.backup(dest)
        assert dest.execute('PRAGMA quick_check').fetchall() == [('ok',)]
finally:
    source.close()
PY
)
echo "Резервная копия: $backup"
changing=1
systemctl stop discord-bot.service
install -d -o www-data -g www-data -m 755 "$app_dir/scripts"
for path in "${files[@]}"; do install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"; done
(
    cd "$app_dir"
    chmod 711 "$stage"
    install -d -o www-data -g www-data -m 700 "$stage/probe"
    DATABASE_URL="sqlite:///$stage/probe/database.sqlite3" PYTHON_DOTENV_DISABLED=1 runuser -u www-data -- ./venv/bin/python -c 'import discord_birthdays; import discord_bot; assert discord_bot.bot.tree.get_command("add_bd")'
    if [[ -n "$import_file" ]]; then
        plan=$(./venv/bin/python scripts/import_birthdays.py "$import_file")
        echo "$plan"
        digest=$(printf '%s' "$plan" | ./venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
        ./venv/bin/python scripts/import_birthdays.py "$import_file" --apply-digest "$digest"
    fi
)
systemctl start discord-bot.service
systemctl is-active discord-bot.service
changing=0
echo 'Готово. Дни рождения: 07:00 МСК. Сайт и сборщики не перезапускались.'
