# Обновление вашего сервера Ubuntu 24.04

Сервер: `132.243.125.99`. Каталог: `/var/www/balance-app`. Сервисы: `balance-app.service` и `discord-bot.service`. Существующую команду Gunicorn с одним worker менять не требуется.

**Первую установку сделайте после завершения текущих игр.** Исходная версия сохраняет их только в памяти, поэтому остановка старого процесса теряет незавершённые игры. Новая версия сохраняет игры в БД и восстанавливает их после следующих перезапусков.

## 1. Загрузить архив из PowerShell

Скачайте `discord-app-fixed.zip` из чата. Если используете файл, уже сохранённый Codex, выполните:

```powershell
scp "C:\Users\Huy\Documents\Codex\2026-10-01\new-chat\outputs\discord-app-fixed.zip" root@132.243.125.99:/tmp/discord-app-fixed.zip
ssh root@132.243.125.99
```

Следующие команды выполняются **в терминале сервера после подключения SSH**, а не в PowerShell.

## 2. Остановить сервисы и сохранить резервную копию

```bash
systemctl stop balance-app.service discord-bot.service
backup_dir="/root/balance-app-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
tar --exclude=venv --exclude=__pycache__ -czf "$backup_dir/app.tar.gz" -C /var/www balance-app
/var/www/balance-app/venv/bin/python -m pip freeze > "$backup_dir/requirements-before.txt"
```

Копия включает текущие файлы, `.env` и `app.db`. Каталог резервной копии доступен только root. Переменная `backup_dir` понадобится для отката, поэтому продолжайте в той же SSH-сессии.

## 3. Установить файлы и зависимости

```bash
python3 -m zipfile -e /tmp/discord-app-fixed.zip /var/www/balance-app
cd /var/www/balance-app
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m compileall -q main.py discord_bot.py database.py runtime.py migrate.py
venv/bin/python migrate.py
```

В архиве нет `.env` и `app.db`, поэтому распаковка их не заменяет. Миграция добавляет недостающие поля и таблицу состояния игр, сохраняя пользователей и баланс.

Если установка или миграция завершилась ошибкой, не переходите к запуску: сохраните вывод и выполните откат ниже либо пришлите ошибку в чат. Миграция отдельно проверяет дубликаты Steam ID и некорректные балансы, чтобы не исправлять ваши данные предположениями.

## 4. Запустить и проверить

```bash
systemctl start balance-app.service discord-bot.service
systemctl status balance-app.service discord-bot.service --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
journalctl -u balance-app.service -u discord-bot.service -n 60 --no-pager
```

Для главной страницы ожидается код `200`.

На сайте проверьте вход через Discord, профиль, одну небольшую ставку и создание покерного стола. В боте — `/me` и `/profile`. После этого можно начать раздачу блэкджека, перезапустить только `balance-app.service` и убедиться, что раздача восстанавливается.

Если сервис не запускается, важен вывод `journalctl`. Перед отправкой удалите из него токены или пароли, если ваши собственные конфигурации их выводят.

## Откат

В той же SSH-сессии, где была задана переменная `backup_dir`:

```bash
systemctl stop balance-app.service discord-bot.service
tar -xzf "$backup_dir/app.tar.gz" -C /var/www
/var/www/balance-app/venv/bin/python -m pip install -r "$backup_dir/requirements-before.txt"
systemctl start balance-app.service discord-bot.service
```

Откат возвращает базу к моменту резервной копии; новые операции после обновления в ней отсутствуют. Если SSH-сессия закрыта, найдите созданный каталог `/root/balance-app-backup-*` и задайте `backup_dir` его точным путём перед выполнением команд отката.
