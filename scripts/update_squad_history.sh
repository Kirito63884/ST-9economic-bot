#!/usr/bin/env bash
# Install group/history views and role collection; preserve live data and secrets.
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

files=(
    main.py
    bulk_operations.py
    squad_history_admin.py
    templates/_admin_squad.html
    templates/_bulk_preview_script.html
    templates/_admin_squad_history.html
    templates/admin.html
    roster_membership.py
    groups_admin.py
    templates/_admin_groups.html
)
services=(balance-app.service discord-bot.service)
case "${3:-full}" in
    full) ;;
    groups-view)
        # Existing Groups installation: website display only, no bot restart.
        files=(squad_history_admin.py groups_admin.py templates/_admin_groups.html)
        services=(balance-app.service)
        test -f "$app_dir/templates/_admin_groups.html"
        ;;
    sqstat-source)
        files=(medals_source.py medals_monitor.py medals.py squad_play_history.py templates/admin.html)
        services=(balance-app.service balance-app-medals.service)
        test -f "$app_dir/medals_monitor.py"
        ;;
    *) echo "Неизвестный режим установки."; exit 1 ;;
esac
python_files=()
for path in "${files[@]}"; do
    if [[ "$path" == *.py ]]; then python_files+=("$path"); fi
done
test -x "$app_dir/venv/bin/python"
test -d "$app_dir/templates"
stage=$(mktemp -d /tmp/squad-history-update.XXXXXX)
changing=0
backup=""
original=()
absent=()

cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ "$changing" == 1 ]]; then
        echo "Ошибка установки. Восстанавливаем прежние файлы."
        systemctl stop "${services[@]}" || true
        tar -xzf "$backup" -C "$app_dir" || return 1
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start "${services[@]}" || true
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
for path in "${files[@]}"; do
    test -f "$stage/code/$path"
    if [[ -f "$app_dir/$path" ]]; then
        original+=("$path")
    else
        absent+=("$path")
    fi
done

# Read-only preflight against the actual deployment configuration.
(
    cd "$app_dir"
    runuser -u www-data -- ./venv/bin/python - <<'PY'
from sqlalchemy import inspect
from database import engine
tables = set(inspect(engine).get_table_names())
required = {"squad_play_sessions", "bulk_confirmations", "discord_member_role_snapshots", "assembly_windows", "assembly_report_checks", "assembly_assessments"}
if not required <= tables:
    raise SystemExit("Не найдены таблицы истории/подтверждений. Сначала требуется предыдущее обновление.")
print("Таблицы истории и подтверждений доступны.")
PY
)

backup="/root/squad-history-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}.tar.gz"
tar -czf "$backup" -C "$app_dir" "${original[@]}"
tar -tzf "$backup" >/dev/null
echo "Резервная копия файлов: $backup"
changing=1
systemctl stop "${services[@]}"
for path in "${files[@]}"; do
    install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"
done
(
    cd "$app_dir"
    runuser -u www-data -- ./venv/bin/python -m py_compile "${python_files[@]}"
)
systemctl start "${services[@]}"
curl -fsS --max-time 10 --retry 5 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active "${services[@]}"
changing=0
if [[ "${3:-full}" == sqstat-source ]]; then
    echo "Готово. Сборщик SQSTAT запущен с адресом https://prot.sqstat.ru. Дата успешного обновления изменится после полного цикла."
else
    echo "Готово. Обновите админку через Ctrl+F5 → Группы. Состав групп обновляется после синхронизации Discord."
fi
