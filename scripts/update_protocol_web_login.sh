#!/usr/bin/env bash
# Install only Protocol authentication; patch shared entry points in place.
set -euo pipefail
umask 077
revision=${1:-}
app_dir=${2:-/var/www/balance-app}
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'Укажите полный SHA коммита.'; exit 1; }
[[ $EUID -eq 0 ]] || { echo 'Запустите от root.'; exit 1; }
test -x "$app_dir/venv/bin/python"
stage=$(mktemp -d /tmp/protocol-web-login.XXXXXX)
backup=""
changing=0
files=(protocol_login.py protocol_login_admin.py templates/_protocol_login.html main.py templates/_admin_audit.html)
original=()
absent=()
cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ $changing == 1 ]]; then
        systemctl stop balance-app.service || true
        tar -xzf "$backup/files.tar.gz" -C "$app_dir"
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start balance-app.service || true
    fi
    echo "Установка прервана. Файлы восстановлены, рабочая база и сессия Protocol не заменялись."
    exit 1
}
trap cleanup EXIT
trap rollback ERR
curl -fL --max-time 120 "https://github.com/Kirito63884/ST-9economic-bot/archive/$revision.tar.gz" -o "$stage/code.tar.gz"
mkdir "$stage/code"
tar -xzf "$stage/code.tar.gz" -C "$stage/code" --strip-components=1
# Patch the installed main/template rather than overwrite other games and tabs.
"$app_dir/venv/bin/python" - "$app_dir" "$stage/code" <<'PY'
import ast
from pathlib import Path
import sys
app, stage = map(Path, sys.argv[1:])
main = (app / 'main.py').read_text()
anchor = '        or request.url.path == "/api/admin/protocol-week-check"'
route = 'app.include_router(make_automation_router(get_user, get_db, is_super_admin))'
if '"/api/admin/protocol-login"' not in main:
    if main.count(anchor) != 1:
        raise SystemExit('Не найдено место подключения запроса без блокировки кошелька.')
    main = main.replace(anchor, anchor + '\n        or request.url.path == "/api/admin/protocol-login"')
if 'from protocol_login_admin import make_router' not in main:
    if main.count(route) != 1:
        raise SystemExit('Не найдено место подключения маршрута.')
    main = main.replace(route, 'from protocol_login_admin import make_router as make_protocol_login_router\napp.include_router(make_protocol_login_router(is_super_admin))\n' + route)
ast.parse(main)
(stage / 'main.py').write_text(main)
template = (app / 'templates/_admin_audit.html').read_text()
if '_protocol_login.html' not in template:
    anchor = '<h2>Загрузка статистики</h2>'
    if template.count(anchor) != 1:
        raise SystemExit('Не найден раздел загрузки статистики. Требуется согласовать установщик.')
    template = template.replace(anchor, anchor + '\n    {% if is_super %}{% include "_protocol_login.html" %}{% endif %}')
(stage / 'templates/_admin_audit.html').write_text(template)
from jinja2 import Environment
for name in ('_admin_audit.html', '_protocol_login.html'):
    Environment().parse((stage / 'templates' / name).read_text())
PY
for path in "${files[@]}"; do
    test -f "$stage/code/$path"
    if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
"$app_dir/venv/bin/python" -m py_compile "$stage/code/main.py" "$stage/code/protocol_login.py" "$stage/code/protocol_login_admin.py"
backup="/root/protocol-web-login-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}"
mkdir -m 700 "$backup"
tar -czf "$backup/files.tar.gz" -C "$app_dir" "${original[@]}"
tar -tzf "$backup/files.tar.gz" >/dev/null
(
    cd "$app_dir"
    ./venv/bin/python - "$backup/database.sqlite3" <<'PY'
import sqlite3
import sys
from database import engine
if engine.dialect.name != 'sqlite':
    raise SystemExit('Этот установщик требует SQLite.')
source = engine.raw_connection()
try:
    with sqlite3.connect(sys.argv[1]) as target:
        source.driver_connection.backup(target)
        assert target.execute('PRAGMA quick_check').fetchall() == [('ok',)]
finally:
    source.close()
PY
)
echo "Резервная копия: $backup"
changing=1
systemctl stop balance-app.service
for path in "${files[@]}"; do
    install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"
done
systemctl start balance-app.service
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/ -o /dev/null
systemctl is-active balance-app.service
changing=0
echo 'Готово. Ctrl+F5 → Админка → Загрузка статистики → Вход в Protocol.'
echo 'Перезапущен только сайт. Получите свежий код в игре командой !сайт.'
