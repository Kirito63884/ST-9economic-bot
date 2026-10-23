"""Consistent online SQLite snapshots. Run independently of the game server."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from database import PROJECT_DIR, engine

NAME_RE = re.compile(r"^st9-\d{8}T\d{12}Z-[a-f0-9]{12}-[a-f0-9]{8}\.db$")
JOB_TIMEOUT = 240


class BackupError(Exception):
    pass


class BackupBusy(BackupError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def source_path():
    if engine.dialect.name != "sqlite" or engine.url.database in (None, "", ":memory:"):
        raise BackupError("Резервирование поддерживает только файловую базу SQLite.")
    return Path(engine.url.database).resolve()


def source_id():
    return hashlib.sha256(str(source_path()).encode()).hexdigest()[:12]


def backup_dir():
    default = (
        "/var/backups/strike-team9" if os.name != "nt" else str(PROJECT_DIR / "backups")
    )
    result = Path(os.getenv("BACKUP_DIR", default)).expanduser().resolve()
    if (
        result == source_path().parent
        or result == result.parent
        or result == PROJECT_DIR
        or PROJECT_DIR in result.parents
        and any(
            path in result.parents or path == result
            for path in (PROJECT_DIR / "templates", PROJECT_DIR / "static")
        )
    ):
        raise BackupError(
            "Каталог копий должен находиться отдельно от файлов приложения."
        )
    return result


def keep_count():
    try:
        count = int(os.getenv("BACKUP_KEEP", "14"))
    except ValueError as exc:
        raise BackupError("BACKUP_KEEP должен быть целым числом от 1 до 365.") from exc
    if not 1 <= count <= 365:
        raise BackupError("BACKUP_KEEP должен быть целым числом от 1 до 365.")
    return count


def _state_path():
    return backup_dir() / ("state-" + source_id() + ".json")


def _read_json(path):
    try:
        if path.is_symlink():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path, data):
    temporary = path.parent / (
        ".partial-" + source_id() + "-" + uuid.uuid4().hex + ".json"
    )
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers briefly deny rename; retry the same atomic file.
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.05)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(directory):
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_partials():
    prefix = ".partial-" + source_id() + "-"
    for path in backup_dir().glob(prefix + "*"):
        if (
            not path.is_symlink()
            and path.is_file()
            and re.fullmatch(
                re.escape(prefix) + r"[a-f0-9]{32}\.(?:json|db)(?:-wal|-shm|-journal)?",
                path.name,
            )
            and time.time() - path.stat().st_mtime > 600
        ):
            path.unlink()


@contextmanager
def backup_lock():
    directory = backup_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (".lock-" + source_id())
    if path.is_symlink():
        raise BackupError("Некорректный файл блокировки резервирования.")
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise BackupBusy(
                        "Копия уже создаётся. Дождитесь завершения."
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise BackupBusy(
                        "Копия уже создаётся. Дождитесь завершения."
                    ) from exc
            locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _job_active(state):
    if state.get("phase") not in ("queued", "running"):
        return False
    try:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(state["started_at"])
        ).total_seconds()
        return age < JOB_TIMEOUT
    except (ValueError, KeyError, TypeError):
        return False


def _file_path(name):
    if (
        not isinstance(name, str)
        or not NAME_RE.fullmatch(name)
        or ("-" + source_id() + "-") not in name
    ):
        raise BackupError("Копия не найдена.")
    path = backup_dir() / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != backup_dir():
        raise BackupError("Копия не найдена.")
    return path


def _manifest(name):
    path = _file_path(name)
    info = _read_json(path.with_suffix(".json"))
    if (
        info.get("name") != name
        or info.get("source_id") != source_id()
        or info.get("version") != 1
        or info.get("bytes") != path.stat().st_size
        or not re.fullmatch(r"[a-f0-9]{64}", str(info.get("sha256", "")))
    ):
        raise BackupError("Метаданные копии повреждены или отсутствуют.")
    try:
        for key in ("created_at", "verified_at"):
            if datetime.fromisoformat(info[key]).tzinfo is None:
                raise ValueError("Missing timezone")
    except (KeyError, ValueError, TypeError) as exc:
        raise BackupError("Дата копии повреждена.") from exc
    return info


def list_backups():
    directory = backup_dir()
    result = []
    if directory.exists():
        for path in directory.glob("st9-*.db"):
            try:
                result.append(_manifest(path.name))
            except (BackupError, OSError):
                continue
    return sorted(result, key=lambda item: item["name"], reverse=True)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity(path):
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("Копия не прошла проверку целостности SQLite.")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"users", "game_history", "app_runtime"}.issubset(tables):
            raise BackupError("В базе отсутствуют обязательные таблицы приложения.")
        for (payload,) in db.execute("SELECT payload FROM app_runtime"):
            json.loads(payload)


def verify_backup(name):
    info = _manifest(name)
    path = _file_path(name)
    if _sha256(path) != info["sha256"]:
        raise BackupError("Контрольная сумма копии не совпадает.")
    _integrity(path)
    return info


def _prune():
    warnings = []
    for info in list_backups()[keep_count() :]:
        try:
            path = _file_path(info["name"])
            path.unlink()
            manifest = path.with_suffix(".json")
            if not manifest.is_symlink():
                manifest.unlink(missing_ok=True)
        except (OSError, BackupError):
            warnings.append("Не удалось удалить старую копию: " + info["name"])
    return "; ".join(warnings)


def _safe_error(exc):
    if isinstance(exc, BackupError):
        return str(exc)
    if isinstance(exc, OSError) and exc.errno == 28:
        return "Недостаточно места для резервной копии. Существующие копии сохранены."
    if isinstance(exc, PermissionError):
        return "Недостаточно прав для чтения базы или записи копии."
    if isinstance(exc, sqlite3.Error):
        message = str(exc).lower()
        if "full" in message:
            return (
                "Недостаточно места для резервной копии. Существующие копии сохранены."
            )
        if "unable to open" in message:
            return "Не удалось открыть базу или файл копии. Проверьте наличие файлов и права www-data."
        if "locked" in message or "busy" in message:
            return "База временно занята. Повторите резервирование позже."
        if "malformed" in message or "not a database" in message:
            return (
                "SQLite обнаружила повреждение базы. Проверьте базу и предыдущие копии."
            )
    return (
        "Не удалось создать копию ("
        + type(exc).__name__
        + "). Проверьте журнал службы резервирования."
    )


def run_backup(trigger="automatic", request_id=None):
    with backup_lock():
        previous = _read_json(_state_path())
        if _job_active(previous) and previous.get("request_id") != request_id:
            raise BackupBusy("Копия уже создаётся. Дождитесь завершения.")
        started = now()
        state = {
            "phase": "running",
            "started_at": started,
            "request_id": request_id,
            "last_success": previous.get("last_success"),
            "trigger": trigger,
        }
        _atomic_json(_state_path(), state)
        temporary = backup_dir() / (
            ".partial-" + source_id() + "-" + uuid.uuid4().hex + ".db"
        )
        final = None
        published = False
        try:
            _cleanup_partials()
            keep_count()  # Validate before publishing or rotating anything.
            source = source_path()
            if not source.is_file():
                raise BackupError(
                    "Файл базы данных не найден. Пустая база не создавалась."
                )
            deadline = time.monotonic() + 180

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise BackupError(
                        "Превышено время создания копии. Повторите позже."
                    )

            with closing(
                sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
            ) as live:
                with closing(sqlite3.connect(temporary)) as copy:
                    os.chmod(temporary, 0o600)
                    live.backup(copy, pages=128, progress=progress, sleep=0.05)
                    copy.execute("PRAGMA journal_mode=DELETE")
            _integrity(temporary)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            name = (
                "st9-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                + "-"
                + source_id()
                + "-"
                + uuid.uuid4().hex[:8]
                + ".db"
            )
            info = {
                "version": 1,
                "source_id": source_id(),
                "name": name,
                "created_at": started,
                "verified_at": now(),
                "bytes": temporary.stat().st_size,
                "sha256": _sha256(temporary),
                "trigger": trigger,
            }
            final = backup_dir() / name
            # A snapshot is downloadable only after its manifest is also published.
            os.replace(temporary, final)
            _atomic_json(final.with_suffix(".json"), info)
            published = True
            warning = _prune()
            state.update(
                phase="success",
                finished_at=now(),
                last_success=info,
                last_error=None,
                warning=warning,
            )
            _atomic_json(_state_path(), state)
            return info
        except Exception as exc:
            state.update(phase="failed", finished_at=now(), last_error=_safe_error(exc))
            try:
                _atomic_json(_state_path(), state)
            except OSError:
                pass
            raise
        finally:
            if final and not published:
                final.unlink(missing_ok=True)
                final.with_suffix(".json").unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm", "-journal"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)


def request_backup():
    with backup_lock():
        previous = _read_json(_state_path())
        if _job_active(previous):
            raise BackupBusy("Копия уже создаётся. Дождитесь завершения.")
        request_id = uuid.uuid4().hex
        state = {
            "phase": "queued",
            "started_at": now(),
            "request_id": request_id,
            "last_success": previous.get("last_success"),
            "trigger": "manual",
        }
        _atomic_json(_state_path(), state)
        try:
            kwargs = (
                {"start_new_session": True}
                if os.name != "nt"
                else {"creationflags": subprocess.CREATE_NO_WINDOW}
            )
            subprocess.Popen(
                [
                    sys.executable,
                    str(PROJECT_DIR / "backup_db.py"),
                    "run",
                    "--trigger",
                    "manual",
                    "--request-id",
                    request_id,
                ],
                cwd=PROJECT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
        except Exception as exc:
            state.update(phase="failed", finished_at=now(), last_error=_safe_error(exc))
            _atomic_json(_state_path(), state)
            raise BackupError(state["last_error"]) from exc
        return request_id


def _unknown_scheduler(reason):
    return {
        "active": None,
        "enabled": None,
        "available": None,
        "error": "Не удалось проверить таймер из процесса сайта.",
        "reason": reason,
    }


def _systemctl_path():
    # systemd web services often set PATH to the virtualenv directory only.
    # Resolve the system tool independently of that environment.
    for path in ("/usr/bin/systemctl", "/bin/systemctl"):
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return shutil.which("systemctl")


def _query_scheduler(executable):
    try:
        response = subprocess.run(
            [
                executable,
                "--no-pager",
                "show",
                "balance-app-backup.timer",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "UnitFileState",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if response.returncode != 0:
            return _unknown_scheduler("query_failed")
        fields = dict(
            line.split("=", 1) for line in response.stdout.splitlines() if "=" in line
        )
        if fields.get("LoadState") == "not-found":
            return {
                "active": False,
                "enabled": False,
                "available": False,
                "error": None,
            }
        if any(
            not fields.get(key) for key in ("LoadState", "ActiveState", "UnitFileState")
        ):
            return _unknown_scheduler("missing_properties")
        return {
            "active": fields["ActiveState"] == "active",
            "enabled": fields["UnitFileState"] in {"enabled", "enabled-runtime"},
            "available": fields["LoadState"] == "loaded",
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return _unknown_scheduler("timeout")
    except OSError:
        return _unknown_scheduler("execution_failed")


def scheduler_status():
    if os.name == "nt":
        return _unknown_scheduler("systemd_unavailable")
    executable = _systemctl_path()
    return (
        _query_scheduler(executable)
        if executable
        else _unknown_scheduler("systemctl_not_found")
    )


def backup_status():
    copies = list_backups()
    state = _read_json(_state_path())
    if state.get("phase") in ("queued", "running") and not _job_active(state):
        state = {
            **state,
            "phase": "failed",
            "last_error": "Создание копии прервано или превысило время ожидания. Можно повторить.",
        }
    return {
        "state": state,
        "files": copies,
        "last_success": copies[0] if copies else None,
        "keep": keep_count(),
        "directory": str(backup_dir()),
        "schedule": scheduler_status(),
        "schedule_label": "Ежедневно в 03:00 по Москве",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "status", "verify"])
    parser.add_argument("name", nargs="?")
    parser.add_argument(
        "--trigger",
        choices=["automatic", "manual", "installation"],
        default="automatic",
    )
    parser.add_argument("--request-id")
    args = parser.parse_args()
    try:
        if args.command == "run":
            # A manually spawned child may start before the parent releases its
            # short reservation lock. Give it time to acquire that lock.
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = run_backup(args.trigger, args.request_id)
                    break
                except BackupBusy:
                    if not args.request_id or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        elif args.command == "verify":
            result = verify_backup(args.name)
        else:
            result = backup_status()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BackupBusy as exc:
        print(str(exc), file=sys.stderr)
        return 0 if args.trigger == "automatic" else 2
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
