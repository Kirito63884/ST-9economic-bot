"""Run interactively on the server; never sends a Steam password to ST9."""

import getpass
import os
import re
import tempfile
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import httpx
from protocol_clan_source import (
    BASE,
    URL,
    ClanUnavailable,
    cookie_path,
    parse_clan,
    request_page,
    safe_url,
)
from protocol_week_source import Tree


def login_form(body):
    tree = Tree()
    tree.feed(body)
    for node in tree.root.walk():
        if node.tag != "form" or node.attrs.get("method", "").upper() != "POST":
            continue
        inputs = [n for n in node.walk() if n.tag == "input"]
        if not any(n.attrs.get("name") == "code" for n in inputs):
            continue
        fields = {
            n.attrs["name"]: n.attrs.get("value", "")
            for n in inputs
            if n.attrs.get("type") == "hidden" and n.attrs.get("name")
        }
        if not any(
            re.fullmatch(r"\$ACTION_ID_[a-f0-9]{40}(?:[a-f0-9]{2})?", k) for k in fields
        ):
            raise ClanUnavailable(
                "Изменился формат входа Protocol. Сессия не изменена."
            )
        return fields
    raise ClanUnavailable("Не найдена форма входа по коду игры.")


def login_with_code(code, before_save=lambda: None):
    """Verify the remote account before atomically replacing the collector session."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ClanUnavailable("Нужен шестизначный код из игры.")
    target = cookie_path()
    jar = MozillaCookieJar()
    login_url = BASE + "/login?next=%2Fclan"
    with httpx.Client(
        cookies=jar,
        timeout=25,
        follow_redirects=False,
        headers={"User-Agent": "StrikeTeam9-ClanStats/1.0"},
    ) as client:
        response = client.get(login_url)
        if response.status_code != 200:
            raise ClanUnavailable(
                "Не удалось открыть форму входа Protocol: HTTP "
                + str(response.status_code)
                + "."
            )
        if len(response.content) > 3_000_000:
            raise ClanUnavailable(
                "Страница входа Protocol превышает допустимый размер."
            )
        fields = login_form(response.text)
        fields.update(code=code, next="/clan?clan=STRIKE TEAM 9")
        response = client.post(
            login_url,
            files={k: (None, v) for k, v in fields.items()},
            headers={"Origin": BASE, "Referer": login_url},
        )
        del code, fields
        if response.status_code not in (200, 303, 307):
            raise ClanUnavailable("Вход не подтверждён. Получите новый код в игре.")
        if response.headers.get("location"):
            from urllib.parse import urljoin

            safe_url(urljoin(login_url, response.headers["location"]))
        document = parse_clan(request_page(client, URL))
        saved = MozillaCookieJar()
        for cookie in client.cookies.jar:
            if cookie.domain.lstrip(".") == "protocol-squad.ru" and cookie.secure:
                saved.set_cookie(cookie)
        if not list(saved):
            raise ClanUnavailable(
                "Protocol не выдал сессию. Сохранённая сессия не изменена."
            )
        before_save()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".protocol-session-", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            saved.save(str(temporary), ignore_discard=True, ignore_expires=True)
            # Root may perform the interactive login for a www-data collector.
            # Atomic replacement must retain the existing service ownership.
            if target.exists() and os.geteuid() == 0:
                owner = target.stat()
                if (owner.st_uid, owner.st_gid) != (os.geteuid(), os.getegid()):
                    os.chown(temporary, owner.st_uid, owner.st_gid)
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return document


def refresh_after_login(document):
    from datetime import timedelta
    from database import SessionLocal
    from protocol_clan_source import ProtocolClanStats, collect_clan, utcnow

    with SessionLocal.begin() as db:
        previous = db.get(ProtocolClanStats, 1)
        if previous:
            previous.attempted_at = utcnow() - timedelta(hours=25)
    # A successful manual login should unblock the next export collection.
    from protocol_award_exports import ProtocolExportHealth

    with SessionLocal.begin() as db:
        health = db.get(ProtocolExportHealth, 1)
        if health:
            health.attempted_at = utcnow() - timedelta(hours=25)
    stored = collect_clan(fetch=lambda: document)
    return stored


def main():
    print("Зайдите на сервер Protocol и напишите в игровом чате !сайт.")
    print(
        "Вход даёт сборщику доступ к вашему кабинету Protocol. Сессия хранится только на этом сервере."
    )
    print("Код вводится скрыто. Пароль Steam не нужен.")
    code = getpass.getpass("Код из игры: ").replace(" ", "")
    document = login_with_code(code)
    del code
    if refresh_after_login(document):
        print(
            f"Вход выполнен. Загружено игроков: {len(document['players'])}. Значения сессии скрыты."
        )
    else:
        print(
            "Вход выполнен, но источник Protocol отключён в настройках автоматизации."
        )


def run_cli():
    try:
        main()
    except ClanUnavailable as exc:
        raise SystemExit("Вход не завершён: " + str(exc)) from None
    except httpx.TimeoutException:
        raise SystemExit(
            "Protocol не ответил вовремя. Проверьте соединение сервера и повторите вход."
        ) from None
    except httpx.HTTPError as exc:
        raise SystemExit(
            "Сетевая ошибка при обращении к Protocol: "
            + type(exc).__name__
            + ". Значения кода и сессии скрыты."
        ) from None
    except OSError:
        raise SystemExit(
            "Не удалось прочитать или сохранить файл сессии. Проверьте права пользователя службы и свободное место."
        ) from None
    except ImportError:
        raise SystemExit(
            "Не хватает зависимости Python для сетевого подключения. Проверьте настройки прокси и установленные зависимости httpx."
        ) from None


if __name__ == "__main__":
    run_cli()
