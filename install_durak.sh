#!/usr/bin/env bash
set -Eeuo pipefail

app_dir=/var/www/balance-app
archive=${1:-/tmp/strike-team9-durak.zip}
python="$app_dir/venv/bin/python"
test -x "$python"
test -f "$app_dir/runtime.py"
test -f "$app_dir/login_links.py"
test -f "$app_dir/poker_history.py"
"$python" -m zipfile -t "$archive"
"$python" - "$archive" <<'PY'
import sys,zipfile
expected={'main.py','discord_bot.py','durak.py','durak_routes.py','install_durak.sh'}
expected.update('templates/'+name for name in ['_site_ui.html','_site_nav.html','_durak_ui.html','durak_lobby.html','durak_table.html','games.html','index.html','profile.html','admin.html','admin_logs.html'])
with zipfile.ZipFile(sys.argv[1]) as z:
    if len(z.infolist()) != len(expected) or set(z.namelist()) != expected:
        raise SystemExit('Unexpected archive contents; installation stopped')
    if any(item.file_size>2_000_000 for item in z.infolist()):
        raise SystemExit('Unexpected file size; installation stopped')
print('Archive contents verified')
PY

backup_dir=$(mktemp -d /root/balance-app-before-durak-$(date +%Y%m%d-%H%M%S)-XXXXXX)
backup_done=0
started=0
finish() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    if [ "$backup_done" -eq 1 ] && [ "$started" -eq 0 ]; then
      echo 'Code verification failed; restoring the backup before restarting services.'
      tar -xzf "$backup_dir/app.tar.gz" -C "$app_dir" || echo 'Backup restoration failed; inspect the archive manually.'
    fi
    systemctl start balance-app.service discord-bot.service || true
    echo "Installation failed. Backup: $backup_dir/app.tar.gz"
    echo 'Inspect: journalctl -u balance-app.service -u discord-bot.service -n 40 --no-pager'
  fi
  exit "$status"
}
trap finish EXIT
systemctl stop balance-app.service discord-bot.service
cd "$app_dir"
tar --exclude='./venv' --exclude='./__pycache__' --exclude='./.git' --exclude='./tests' --exclude='*.pyc' -czf "$backup_dir/app.tar.gz" .
backup_done=1
echo "Backup: $backup_dir/app.tar.gz"
"$python" -m zipfile -e "$archive" "$app_dir"
chown www-data:www-data main.py discord_bot.py durak.py durak_routes.py templates/*.html
chmod 644 main.py discord_bot.py durak.py durak_routes.py templates/*.html
"$python" -m compileall -q main.py discord_bot.py durak.py durak_routes.py
"$python" - <<'PY'
from pathlib import Path
from jinja2 import Environment,FileSystemLoader
env=Environment(loader=FileSystemLoader('templates'))
for path in Path('templates').glob('*.html'):
    env.get_template(path.name)
import main
assert '/api/durak/tables' in main.app.openapi()['paths']
print('Python imports, routes and templates verified')
PY

# After services start, never replace the database with an older backup automatically.
started=1
systemctl start balance-app.service discord-bot.service
for attempt in $(seq 1 20); do
  if curl --silent --fail http://127.0.0.1:8000/ -o /dev/null; then
    systemctl is-active --quiet balance-app.service
    systemctl is-active --quiet discord-bot.service
    echo 'Update installed. Open https://striketeam9bank.ru/games/durak/lobby and press Ctrl+F5.'
    echo "Backup: $backup_dir/app.tar.gz"
    exit 0
  fi
  sleep 1
done
echo 'The website has not responded. Check the service logs; the current database is preserved.'
exit 1
