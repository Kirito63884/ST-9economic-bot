"""Preview/import a private JSON registry; never overwrite existing dates silently."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from discord_birthdays import REGISTRY_KEY, read, validate_registry, parse_date
from roster_settings import write
from runtime import initialize_database, metadata_transaction


def import_plan(current, incoming):
    conflicts = [
        uid
        for uid, row in incoming.items()
        if uid in current and current[uid]["date"] != row["date"]
    ]
    if conflicts:
        raise ValueError(
            f"Конфликт дат в {len(conflicts)} записях. Прежние данные сохранены; исправьте файл вручную."
        )
    additions = {uid: row for uid, row in incoming.items() if uid not in current}
    digest = hashlib.sha256(
        json.dumps([current, incoming], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return additions, digest


async def import_file(path, expected=None):
    incoming = validate_registry(json.loads(path.read_text(encoding="utf-8-sig")))
    paused = 0
    for record in incoming.values():
        try:
            parse_date(record["date"])
        except ValueError:
            paused += 1
    async with metadata_transaction() as db:
        current = read(db, REGISTRY_KEY, {})
        additions, digest = import_plan(current, incoming)
        if expected is not None:
            if digest != expected:
                raise ValueError("План изменился. Повторите предварительную проверку.")
            write(db, REGISTRY_KEY, {**current, **additions})
        return {
            "records": len(incoming),
            "paused": paused,
            "additions": len(additions),
            "existing": len(incoming) - len(additions),
            "digest": digest,
            "applied": expected is not None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--apply-digest")
    args = parser.parse_args()
    initialize_database()
    try:
        result = asyncio.run(import_file(args.file, args.apply_digest))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
