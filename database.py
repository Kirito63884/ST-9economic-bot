"""Shared schema and database configuration for the website and Discord bot."""

import os
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Text,
    Index,
    UniqueConstraint,
    text,
    inspect,
    event,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.sql import func
from sqlalchemy.engine import make_url

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite:///{(PROJECT_DIR / 'app.db').as_posix()}"
)
url = make_url(DATABASE_URL)
if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
    database_path = Path(url.database)
    if not database_path.is_absolute():
        url = url.set(database=str(PROJECT_DIR / database_path))
connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if url.get_backend_name() == "sqlite"
    else {}
)
engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}
    id = Column(Integer, primary_key=True)
    discord_id = Column(String, unique=True, nullable=False)
    username = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    guild_nickname = Column(String, nullable=True)
    balance = Column(Float, default=1000.0)
    is_admin = Column(Boolean, default=False)
    is_senior_admin = Column(Boolean, default=False)
    is_chief_admin = Column(Boolean, default=False)
    is_tech_admin = Column(Boolean, default=False)
    auth_version = Column(Integer, default=0)
    profile_public = Column(Boolean, default=True)
    bio = Column(String(500), nullable=True)
    favorite_game = Column(String, nullable=True)
    avatar_emoji = Column(String, nullable=True)
    quote = Column(String(200), nullable=True)
    theme_color = Column(String, nullable=True)
    admin_notes = Column(String(1000), nullable=True)
    last_daily_bonus = Column(DateTime, nullable=True)
    daily_streak = Column(Integer, default=0)
    last_seen = Column(DateTime, nullable=True)
    steam_id = Column(String(50), nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    registered_at = Column(
        DateTime,
        nullable=True,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


class GameHistory(Base):
    __tablename__ = "game_history"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    game_type = Column(String, nullable=False)
    result = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    details = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class AppRuntime(Base):
    """One locked row makes the wallet and game state one atomic transaction."""

    __tablename__ = "app_runtime"
    id = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=False, default="{}")
    secret_key = Column(String(128), nullable=False)
    next_user_id = Column(Integer, nullable=False, default=1)


class DiscordSyncPending(Base):
    """Durable, coalesced notifications; Discord remains the membership authority."""

    __tablename__ = "discord_sync_pending"
    discord_id = Column(String(20), primary_key=True)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False)
    retry_at = Column(DateTime, nullable=True, index=True)


class PokerHand(Base):
    __tablename__ = "poker_hands"
    __table_args__ = (UniqueConstraint("table_id", "hand_number"),)
    id = Column(Integer, primary_key=True)
    table_id = Column(String(64), nullable=False, index=True)
    hand_number = Column(Integer, nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PokerHandPlayer(Base):
    __tablename__ = "poker_hand_players"
    hand_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, primary_key=True, index=True)


class LoginLink(Base):
    """Only a digest is stored; the bearer secret is returned once to its issuer."""

    __tablename__ = "login_links"
    token_hash = Column(String(64), primary_key=True)
    discord_id = Column(String(20), nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    source = Column(String(10), nullable=False)
    issued_by = Column(String(20), nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


STEAM_ID_INDEX = Index("uq_users_steam_id", User.steam_id, unique=True)


class InvalidBalance(ValueError):
    pass


@event.listens_for(SessionLocal, "before_flush")
def validate_wallets(db, flush_context, instances):
    for entity in list(db.new) + list(db.dirty):
        if isinstance(entity, User) and entity.balance is not None:
            if (
                not math.isfinite(entity.balance)
                or not -1_000_000_000_000 <= entity.balance <= 1_000_000_000_000
            ):
                raise InvalidBalance("Баланс вне допустимого диапазона")
            entity.balance = float(
                Decimal(str(entity.balance)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        if isinstance(entity, GameHistory):
            if not math.isfinite(entity.amount):
                raise InvalidBalance("Некорректная сумма в истории")
            entity.amount = float(
                Decimal(str(entity.amount)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )


def run_migrations():
    """Add missing user columns using the database's own SQL types."""
    Base.metadata.create_all(bind=engine)
    # Early quiz builds did not yet have a separately editable source quote.
    if inspect(engine).has_table("server_rule_questions"):
        rule_columns = {
            column["name"]
            for column in inspect(engine).get_columns("server_rule_questions")
        }
        if "source_quote" not in rule_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE server_rule_questions ADD COLUMN source_quote TEXT"
                    )
                )
    existing = {column["name"] for column in inspect(engine).get_columns("users")}
    defaults = {
        "profile_public": True,
        "is_admin": False,
        "is_senior_admin": False,
        "is_chief_admin": False,
        "is_tech_admin": False,
        "auth_version": 0,
        "daily_streak": 0,
        "balance": 1000.0,
    }
    with engine.begin() as connection:
        for column in User.__table__.columns:
            if column.name in existing:
                continue
            if column.name in {"id", "discord_id", "username"}:
                raise RuntimeError(f"Incompatible users table: missing {column.name}")
            sql_type = column.type.compile(dialect=engine.dialect)
            definition = f"ALTER TABLE users ADD COLUMN {column.name} {sql_type}"
            if column.name in defaults:
                value = defaults[column.name]
                literal = (
                    ("TRUE" if value else "FALSE")
                    if isinstance(value, bool)
                    else str(value)
                )
                definition += f" DEFAULT {literal}"
            connection.execute(text(definition))
        for name, value in defaults.items():
            connection.execute(
                text(f"UPDATE users SET {name} = :value WHERE {name} IS NULL"),
                {"value": value},
            )
        connection.execute(text("UPDATE users SET steam_id = NULL WHERE steam_id = ''"))
    # The website and bot may bind an id concurrently; an ORM pre-check is insufficient.
    STEAM_ID_INDEX.create(bind=engine, checkfirst=True)
