#!/usr/bin/env bash
# UI-only navigation update; preserve live data, bot and source collectors.
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
test -f "$app_dir/templates/_admin_group_reports.html"
files=(main.py templates/admin.html templates/_admin_audit.html templates/_admin_hub.html templates/_statistics_freshness.html)
stage=$(mktemp -d /tmp/admin-navigation.XXXXXX)
changing=0
backup=""
original=()
absent=()
cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ "$changing" == 1 ]]; then
        echo "Ошибка установки. Восстанавливаем прежние файлы сайта."
        systemctl stop balance-app.service || true
        tar -xzf "$backup" -C "$app_dir" || return 1
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start balance-app.service || true
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
    if [[ -f "$app_dir/$path" ]]; then original+=("$path"); else absent+=("$path"); fi
done
"$app_dir/venv/bin/python" -m py_compile "$stage/code/main.py"
backup="/root/admin-navigation-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}.tar.gz"
tar -czf "$backup" -C "$app_dir" "${original[@]}"
tar -tzf "$backup" >/dev/null
echo "Резервная копия: $backup"
changing=1
systemctl stop balance-app.service
for path in "${files[@]}"; do
    install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"
done
systemctl start balance-app.service
curl -fsS --max-time 10 --retry 6 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1:8000/ -o /dev/null
systemctl is-active balance-app.service
changing=0
echo "Готово. Ctrl+F5: группы меню сворачиваются, журнал и статистика открываются внутри админки."
echo "Discord-бот и сборщики не перезапускались."
