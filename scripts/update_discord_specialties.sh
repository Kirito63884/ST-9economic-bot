#!/usr/bin/env bash
# Install only the Discord specialty panel, preserving secrets, data and workers.
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
test -f "$app_dir/discord_bot.py"
files=(discord_bot.py discord_specialties.py)
original=()
absent=()
stage=$(mktemp -d /tmp/discord-specialties.XXXXXX)
changing=0
backup=""
cleanup() { rm -rf "$stage"; }
rollback() {
    trap - ERR
    if [[ "$changing" == 1 ]]; then
        echo "Ошибка установки. Восстанавливаем прежние файлы бота."
        systemctl stop discord-bot.service || true
        tar -xzf "$backup" -C "$app_dir" || return 1
        for path in "${absent[@]}"; do rm -f "$app_dir/$path"; done
        systemctl start discord-bot.service || true
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
# Compile before stopping the bot. No database migrations or dependency changes.
"$app_dir/venv/bin/python" -m py_compile "${files[@]/#/$stage/code/}"
backup="/root/discord-specialties-before-$(date +%Y%m%d-%H%M%S)-${stage##*.}.tar.gz"
tar -czf "$backup" -C "$app_dir" "${original[@]}"
tar -tzf "$backup" >/dev/null
echo "Резервная копия: $backup"
changing=1
systemctl stop discord-bot.service
for path in "${files[@]}"; do
    install -o www-data -g www-data -m 644 "$stage/code/$path" "$app_dir/$path"
done
(
    cd "$app_dir"
    runuser -u www-data -- ./venv/bin/python -m py_compile "${files[@]}"
)
systemctl start discord-bot.service
sleep 3
systemctl is-active discord-bot.service
changing=0
echo "Код установлен. После подключения бота появится панель в канале 1448264624242819093."
echo "Проверьте журнал: journalctl -u discord-bot.service --since '3 minutes ago' --no-pager -n 70"
echo "Успешная публикация отмечается строкой Specialty panel ready. Статус active сам по себе её не подтверждает."
