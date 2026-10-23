"""Run once with both services stopped before deploying a schema change."""

import math
from sqlalchemy import inspect, text
from database import engine


def main():
    if "users" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("users")}
        with engine.connect() as connection:
            if "steam_id" in columns:
                duplicates = connection.execute(
                    text(
                        "SELECT steam_id FROM users WHERE steam_id IS NOT NULL AND steam_id != '' "
                        "GROUP BY steam_id HAVING COUNT(*) > 1"
                    )
                ).all()
                if duplicates:
                    raise SystemExit(
                        f"Найдены дубликаты Steam ID: {len(duplicates)}. "
                        "Устраните их перед миграцией; данные не изменены."
                    )
            if "balance" in columns:
                invalid = [
                    row.id
                    for row in connection.execute(text("SELECT id, balance FROM users"))
                    if row.balance is not None
                    and (
                        not math.isfinite(row.balance)
                        or not -1_000_000_000_000 <= row.balance <= 1_000_000_000_000
                    )
                ]
                if invalid:
                    raise SystemExit(
                        f"Некорректный баланс у пользователей ID: {invalid}. "
                        "Исправьте значения перед миграцией; данные не изменены."
                    )
    from runtime import initialize_database
    import medals  # Register permanent award/statistics tables before migration.  # noqa: F401 - Register shared ORM tables before schema creation.

    import rule_tests  # noqa: F401 — Register assigned tests before creating tables.

    initialize_database()
    print("Миграция завершена. Пользователи и балансы сохранены.")


if __name__ == "__main__":
    main()
