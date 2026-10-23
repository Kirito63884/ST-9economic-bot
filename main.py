import os
import secrets

import itertools
import time
import asyncio
import math
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from contextlib import asynccontextmanager
import logging
import json
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
import httpx

# Wallet outcomes use operating-system entropy, not a predictable PRNG.
random = secrets.SystemRandom()

load_dotenv(Path(__file__).resolve().parent / ".env")

# === Конфигурация ===
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
ADMIN_DISCORD_ID = os.getenv("ADMIN_DISCORD_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
DISCORD_API_BASE = os.getenv("DISCORD_API_BASE", "https://discord.com")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
COOKIE_HTTPS_ONLY = os.getenv(
    "COOKIE_HTTPS_ONLY",
    "true"
    if urlsplit(os.getenv("SITE_URL") or DISCORD_REDIRECT_URI or "").scheme == "https"
    else "false",
).lower() in {
    "1",
    "true",
    "yes",
}
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
REQUIRED_GUILD_ID = os.getenv("REQUIRED_GUILD_ID")
REQUIRED_ROLE_ID = os.getenv("REQUIRED_ROLE_ID")
GUILD_NAME = os.getenv("GUILD_NAME", "наш сервер")
ROLE_NAME = os.getenv("ROLE_NAME", "определённую роль")
GUILD_INVITE_URL = os.getenv("GUILD_INVITE_URL", "")

# === Прокси для обхода блокировки Discord в РФ ===
DISCORD_BUGS_WEBHOOK = os.getenv("DISCORD_BUGS_WEBHOOK")
DISCORD_PROXY_TOKEN = os.getenv("DISCORD_PROXY_TOKEN")
DISCORD_API_PROXY = os.getenv("DISCORD_API_PROXY")  # Cloudflare Worker URL

from database import (
    PROJECT_DIR,
    User,
    GameHistory,
    LoginLink,
    InvalidBalance,
    PokerHand,
    PokerHandPlayer,
    SessionLocal,
)
from poker_history import archive_hand, visible_hand, anonymize_player
from table_admin import management as table_management
from durak import DurakError
from durak_routes import DurakService, build_router as build_durak_router
from billiards import BilliardsError
from billiards_routes import BilliardsService, build_router as build_billiards_router
from bulk_operations import bulk_preview
from runtime import (
    initialize_database,
    transaction,
    current_db,
    current_runtime,
    metadata_transaction,
)
from discord_sync import sync_profile_name
from audit_logs import is_money_log, log_details, page_context as audit_page_context
from squad_names import admin_game_names
from medals import visible_awards, sync_manual
from medals_routes import medal_checker, make_router as make_medals_router
from automation_admin import make_router as make_automation_router
from admin_hub import make_router as make_admin_hub_router
from roster_membership import (
    RosterMembership,
    cargo_role_id,
    cargo_user_ids,
    is_cargo_user,
    user_role_context,
)
from squad_play_history import profile_history, queue_history_refresh
from server_rules_questions import (
    ServerRuleQuestion,
    apply_question,
    list_server_rule_questions,
    normalize_question_payload,
    question_record,
    utcnow as rules_utcnow,
)
from discord_event_log import (
    DiscordChannelCatalog,
    DiscordEventLog,
    DiscordVoicePresence,
)
from database import DiscordSyncPending
from login_links import (
    LoginLinkError,
    issue_login_link,
    get_valid_login_link,
    revoke_login_links,
    validate_discord_id,
    site_origin,
    utcnow as link_utcnow,
)


from rule_tests import (
    make_router as make_rule_tests_router,
    player_summaries,
    can_manage_tests,
)

# === FastAPI ===
SECRET_KEY = SECRET_KEY or initialize_database()
if os.getenv("SECRET_KEY"):
    initialize_database()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    tasks = [
        asyncio.create_task(poker_timeout_checker()),
        asyncio.create_task(role_checker_task()),
        asyncio.create_task(discord_event_checker()),
        asyncio.create_task(game_settlement_checker()),
        asyncio.create_task(durak_timeout_checker()),
        asyncio.create_task(billiards_timeout_checker()),
        asyncio.create_task(squad_schedule_checker()),
        asyncio.create_task(medal_checker()),
    ]
    app.state.background_tasks = tasks
    try:
        yield
    finally:
        all_tasks = tasks + list(manual_role_tasks)
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(directory=str(PROJECT_DIR / "templates"))
templates.env.globals.update(is_money_log=is_money_log, log_details=log_details)

MOSCOW_TZ = timezone(timedelta(hours=3))


def format_moscow_datetime(value):
    """Render UTC database timestamps explicitly in Moscow time."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M МСК")


templates.env.filters["moscow_datetime"] = format_moscow_datetime

# === Глобальные состояния игр ===
active_games = {}
poker_tables = {}
table_requests = {}
crash_games = {}
durak_service = DurakService()
billiards_service = BilliardsService()


@app.exception_handler(DurakError)
async def durak_error_handler(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(BilliardsError)
async def billiards_error_handler(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


def is_finite_number(value) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def is_cent_amount(value):
    return (
        is_finite_number(value)
        and abs(float(value)) <= 1_000_000_000_000
        and abs(float(value) * 100 - round(float(value) * 100)) < 1e-6
    )


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class RejectedResponse(Exception):
    def __init__(self, response):
        self.response = response


@app.middleware("http")
async def durable_request(request: Request, call_next):
    # A synchronizer token is required for authenticated browser mutations.
    # Origin checks remain an independent layer, including same-site subdomains.
    uid = request.session.get("user_id")
    if uid:
        csrf = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path != "/auth/link"
        ):
            supplied = request.headers.get("X-ST9-CSRF", "")
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if not supplied and content_type in {
                "application/x-www-form-urlencoded",
                "multipart/form-data",
            }:
                await request.body()
                supplied = str((await request.form()).get("csrf_token", ""))
            if not supplied or not secrets.compare_digest(supplied, csrf):
                return JSONResponse(
                    {
                        "detail": "Обновите страницу: проверка безопасности запроса не пройдена."
                    },
                    status_code=403,
                )
    # Browser mutations require the origin of the website. Non-browser clients
    # can use the API without an Origin header and do not inherit session cookies.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if not origin and request.headers.get("referer"):
            referer = urlsplit(request.headers["referer"])
            origin = f"{referer.scheme}://{referer.netloc}"
        # Nginx may forward a local HTTP URL, and the OAuth callback can use a
        # different host. SITE_URL is the configured browser-facing origin.
        public_url = os.getenv("SITE_URL") or DISCORD_REDIRECT_URI
        expected = urlsplit(public_url) if public_url else request.url
        expected_origin = f"{expected.scheme}://{expected.netloc}"
        if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
            request_guard.rejection("403-origin")
            logger.debug(
                "Request origin rejected: method=%s origin=%r expected=%r fetch_site=%r",
                request.method,
                origin[:256],
                expected_origin[:256],
                request.headers.get("sec-fetch-site", "")[:64],
            )
            return JSONResponse(
                {"detail": "Запрос с другого сайта запрещён"}, status_code=403
            )
        if not origin and request.headers.get("sec-fetch-site") == "cross-site":
            request_guard.rejection("403-origin")
            logger.debug(
                "Cross-site request rejected without Origin: method=%s expected=%r",
                request.method,
                expected_origin[:256],
            )
            return JSONResponse(
                {"detail": "Запрос с другого сайта запрещён"}, status_code=403
            )
    # Backup routes authenticate through a separate read-only session. They must
    # not take the wallet/runtime write lock, especially while streaming a file.
    if (
        request.url.path
        in {
            "/api/admin/player-details",
            "/api/admin/player-squad-history",
            "/api/admin/discord-roster",
            "/api/admin/assembly-status",
            "/api/admin/automation/reconcile",
            "/api/admin/roster/sync",
            "/api/admin/assembly-reports",
            "/api/admin/backups",
        }
        or request.url.path in {"/api/admin/player-presence", "/api/site-presence"}
        or request.url.path.startswith("/api/admin/award-design")
        or request.url.path == "/api/admin/protocol-week-check"
        or (
            request.method == "GET"
            and request.url.path.startswith("/api/admin/assembly/")
            and request.url.path.endswith(("/absent.txt", "/review.txt"))
        )
        or request.url.path.startswith("/award-images/")
        or request.url.path.startswith("/api/admin/backups/")
        or request.url.path.startswith("/api/admin/player-squad-history/")
        or request.url.path.startswith("/api/admin/player-voice-history/")
    ):
        return await call_next(request)
    if request.url.path in {
        "/favicon.svg",
        "/login",
        "/auth/callback",
        "/access-denied",
        "/api/bug-report",
        "/auth/link",
        "/api/admin/login-links",
        "/api/admin/medals/discord-names",
    }:
        return await call_next(request)
    # A slow request body must not hold the shared database/game lock.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        await request.body()
    try:
        async with transaction(serialize_runtime, restore_runtime) as db:
            response = await call_next(request)
            if response.status_code >= 400:
                raise RejectedResponse(response)
            uid = request.session.get("user_id")
            if uid:
                user = (
                    db.query(User)
                    .filter(User.deleted_at.is_(None))
                    .filter(User.id == uid, User.deleted_at.is_(None))
                    .first()
                )
                now = datetime.now()
                if user and (
                    not user.last_seen or (now - user.last_seen).total_seconds() >= 60
                ):
                    user.last_seen = now
            # Guard the persisted wallet against nonfinite or out-of-range values.
            for entity in list(db.new) + list(db.dirty):
                if isinstance(entity, User) and entity.balance is not None:
                    if (
                        not is_finite_number(entity.balance)
                        or not MIN_BALANCE <= entity.balance <= MAX_BALANCE
                    ):
                        raise HTTPException(400, "Баланс вне допустимого диапазона")
            return response
    except RejectedResponse as rejected:
        return rejected.response
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except IntegrityError:
        return JSONResponse(
            {"detail": "Данные конфликтуют с существующей записью"}, status_code=409
        )
    except InvalidBalance as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


# === Вспомогательные функции ===
def get_discord_redirect_uri(request: Request) -> str:
    if DISCORD_REDIRECT_URI:
        return DISCORD_REDIRECT_URI
    return str(request.url.replace(path="/auth/callback", query=""))


async def get_db():
    db = current_db.get()
    if db is None:
        raise RuntimeError("Database access requires a runtime transaction")
    yield db


MAX_BALANCE = 1_000_000_000_000.0
MIN_BALANCE = -MAX_BALANCE


def sync_table_balances(db: Session, table):
    users = {
        u.id: u
        for u in db.query(User)
        .filter(User.id.in_([p["user_id"] for p in table.players]))
        .all()
    }
    for player in table.players:
        user = users.get(player["user_id"])
        if user:
            user.balance += player["balance"] - player.get(
                "synced_balance", player["balance"]
            )
            player["synced_balance"] = player["balance"]
            if (
                table.hand_payout_done
                and not player.get("history_logged")
                and player.get("participated")
            ):
                delta = player.get("returned", 0) - player["invested"]
                result = "won" if delta > 1e-9 else "lost" if delta < -1e-9 else "push"
                log_game(
                    db,
                    user.id,
                    "poker",
                    result,
                    delta,
                    f"Стол {table.id}, раздача #{table.hand_number}",
                )
                player["history_logged"] = True
    archive_hand(db, table)
    db.flush()


def refresh_table_balances(db, table):
    users = {
        u.id: u
        for u in db.query(User)
        .filter(User.id.in_([p["user_id"] for p in table.players]))
        .all()
    }
    for player in table.players:
        user = users.get(player["user_id"])
        if user:
            player["balance"] = player["synced_balance"] = float(user.balance)
            player["username"] = user.username
            player["display_name"] = (
                user.display_name or user.guild_nickname or user.username
            )


def ensure_one_table(user_id, except_table=None):
    if any(
        table.id != except_table and any(p["user_id"] == user_id for p in table.players)
        for table in poker_tables.values()
    ):
        raise HTTPException(409, "Сначала покиньте другой покерный стол")


async def get_user(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    return (
        db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
        if uid
        else None
    )


def require_admin(user=Depends(get_user)):
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Нет прав администратора")
    return user


def is_owner_admin(user):
    return user and user.discord_id == ADMIN_DISCORD_ID


def is_super_admin(user):
    return bool(
        user and (is_owner_admin(user) or (user.is_admin and user.is_chief_admin))
    )


def can_edit_user(actor, target):
    return bool(
        actor and target and (not is_owner_admin(target) or is_owner_admin(actor))
    )


def protect_owner(actor, target):
    if not can_edit_user(actor, target):
        raise HTTPException(
            403,
            "Аккаунт владельца защищён: менять его баланс, ник и права может только он сам.",
        )


def is_senior_or_super(user):
    return user and (user.is_senior_admin or is_super_admin(user))


def log_game(
    db: Session,
    user_id: int,
    game_type: str,
    result: str,
    amount: float,
    details: str = "",
):
    entry = GameHistory(
        user_id=user_id,
        game_type=game_type,
        result=result,
        amount=amount,
        details=details,
    )
    db.add(entry)
    db.flush()


# === Discord API через прокси (обход блокировки) ===
def get_discord_client(timeout=15.0):
    """Создаёт httpx.AsyncClient с заголовком прокси"""
    headers = {}
    if DISCORD_PROXY_TOKEN:
        headers["X-Proxy-Token"] = DISCORD_PROXY_TOKEN
    return httpx.AsyncClient(timeout=timeout, headers=headers)


def get_discord_api_url(endpoint: str) -> str:
    """Возвращает URL для Discord API (через прокси если настроен)"""
    if DISCORD_API_PROXY:
        return f"{DISCORD_API_PROXY.rstrip('/')}/api{endpoint}"
    return f"{DISCORD_API_BASE.rstrip('/')}/api{endpoint}"


async def check_discord_membership(discord_id: str) -> dict:
    result = {"allowed": True, "guild_nickname": None, "username": None, "roles": []}
    if not REQUIRED_GUILD_ID and not REQUIRED_ROLE_ID:
        return result
    if not REQUIRED_GUILD_ID or not DISCORD_BOT_TOKEN:
        raise HTTPException(503, "Проверка доступа Discord не настроена")
    try:
        async with get_discord_client(timeout=20) as client:
            response = await client.get(
                get_discord_api_url(
                    f"/guilds/{REQUIRED_GUILD_ID}/members/{discord_id}"
                ),
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
        if response.status_code == 404:
            return {
                **result,
                "allowed": False,
                "reason": "not_member",
                "message": f"Вы не являетесь участником сервера {GUILD_NAME}",
            }
        if response.status_code != 200:
            raise HTTPException(
                503, "Discord временно не позволяет проверить доступ. Попробуйте позже"
            )
        member = response.json()
        roles = member.get("roles")
        if not isinstance(roles, list):
            raise HTTPException(503, "Некорректный ответ Discord")
        result["roles"] = roles
        result["username"] = member.get("user", {}).get("username")
        result["guild_nickname"] = (
            member.get("nick")
            or member.get("user", {}).get("global_name")
            or member.get("user", {}).get("username")
        )
        with SessionLocal() as db:
            retired_role = cargo_role_id(db)
        if retired_role and retired_role in {str(role) for role in roles}:
            return {
                **result,
                "allowed": False,
                "reason": "cargo200",
                "message": "Профиль перенесён в категорию «Груз 200» и доступен только для просмотра администраторами",
            }
        if REQUIRED_ROLE_ID and REQUIRED_ROLE_ID not in roles:
            return {
                **result,
                "allowed": False,
                "reason": "no_role",
                "message": f"У вас нет роли {ROLE_NAME} на сервере {GUILD_NAME}",
            }
        return result
    except (httpx.RequestError, ValueError, TypeError):
        logger.warning("Discord membership verification unavailable")
        raise HTTPException(
            503, "Не удалось проверить доступ через Discord. Попробуйте позже"
        )


async def fetch_required_role_members() -> list[dict]:
    """Return every non-bot guild member who currently has the required role."""
    if not REQUIRED_GUILD_ID or not REQUIRED_ROLE_ID or not DISCORD_BOT_TOKEN:
        raise HTTPException(503, "Проверка участников Discord не настроена")
    members: list[dict] = []
    with SessionLocal() as db:
        retired_role = cargo_role_id(db)
    after = "0"
    try:
        async with get_discord_client(timeout=25) as client:
            for _ in range(250):
                response = await client.get(
                    get_discord_api_url(f"/guilds/{REQUIRED_GUILD_ID}/members"),
                    params={"limit": 1000, "after": after},
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
                if response.status_code != 200:
                    logger.warning(
                        "Discord roster request failed with status %s",
                        response.status_code,
                    )
                    raise HTTPException(
                        503,
                        "Discord временно не отдаёт список участников. Попробуйте позже",
                    )
                page = response.json()
                if not isinstance(page, list):
                    raise HTTPException(
                        503, "Discord вернул некорректный список участников"
                    )
                for member in page:
                    if not isinstance(member, dict):
                        continue
                    user = member.get("user")
                    roles = member.get("roles")
                    if (
                        not isinstance(user, dict)
                        or user.get("bot")
                        or not isinstance(roles, list)
                        or REQUIRED_ROLE_ID not in roles
                        or (
                            retired_role
                            and retired_role in {str(role) for role in roles}
                        )
                    ):
                        continue
                    discord_id = str(user.get("id") or "")
                    if not discord_id.isdigit():
                        continue
                    username = str(user.get("username") or discord_id)
                    display_name = str(
                        member.get("nick") or user.get("global_name") or username
                    )
                    members.append(
                        {
                            "discord_id": discord_id,
                            "username": username,
                            "display_name": display_name,
                        }
                    )
                if len(page) < 1000:
                    break
                last = page[-1] if page else None
                last_user = (last or {}).get("user")
                next_after = str(
                    last_user.get("id") if isinstance(last_user, dict) else ""
                )
                if not next_after.isdigit() or next_after == after:
                    raise HTTPException(
                        503, "Discord вернул неполный список участников"
                    )
                after = next_after
            else:
                raise HTTPException(503, "Список участников Discord слишком большой")
    except HTTPException:
        raise
    except (httpx.RequestError, ValueError, TypeError):
        logger.warning("Discord roster unavailable", exc_info=True)
        raise HTTPException(
            503, "Не удалось получить список участников Discord. Попробуйте позже"
        )
    members.sort(key=lambda row: (row["display_name"].casefold(), row["discord_id"]))
    return members


def get_user_activity(user_id: int) -> dict:
    activity = {"game": None, "details": None}
    for table in billiards_service.tables.values():
        player = table.player(user_id)
        if player and not player.get("left") and table.phase == "playing":
            return {
                "game": "billiards",
                "details": (
                    "Бильярд · тренировка"
                    if table.rules["mode"] == "practice"
                    else f"Бильярд · банк {table.match['bank_cents'] / 100:.2f} БК"
                ),
            }
    for table in durak_service.tables.values():
        player = table.player(user_id)
        if (
            player
            and not player.get("left")
            and not player["out"]
            and table.phase not in ("waiting", "finished")
        ):
            return {
                "game": "durak",
                "details": f"Дурак · банк {table.match['bank_cents'] / 100:.2f} БК",
            }
    if user_id in active_games:
        game = active_games[user_id]
        if game.get("status") == "playing":
            activity["game"] = "blackjack"
            activity["details"] = f"Ставка: {game['bet']:.2f}"
            return activity
    if user_id in crash_games:
        game = crash_games[user_id]
        if game.status == "running":
            activity["game"] = "crash"
            activity["details"] = (
                f"Ставка: {game.bet:.2f} | x{game.get_current_multiplier():.2f}"
            )
            return activity
    for table in poker_tables.values():
        for player in table.players:
            if player["user_id"] == user_id:
                activity["game"] = "poker"
                activity["details"] = f"Банк: {table.pot:.2f}"
                return activity
    return activity


def ensure_discord_profile(db, discord_id, username, guild_nickname):
    """All login methods resolve one Discord identity to the same website profile."""
    user = db.query(User).filter(User.discord_id == discord_id).first()
    is_owner = bool(ADMIN_DISCORD_ID and discord_id == ADMIN_DISCORD_ID)
    if user is None:
        if not username:
            raise HTTPException(
                503, "Не удалось получить Discord-профиль. Попробуйте позже"
            )
        row = current_runtime.get()
        highest_id = max(
            db.query(func.max(User.id)).scalar() or 0,
            db.query(func.max(GameHistory.user_id)).scalar() or 0,
        )
        next_id = max(row.next_user_id, highest_id + 1)
        row.next_user_id = next_id + 1
        user = User(
            id=next_id,
            discord_id=discord_id,
            username=username,
            display_name=guild_nickname or username,
            guild_nickname=guild_nickname or username,
            is_admin=is_owner,
            is_senior_admin=is_owner,
        )
        db.add(user)
    else:
        if user.deleted_at:
            user.deleted_at = None
            log_game(
                db,
                user.id,
                "admin",
                "user_restored",
                0,
                "Возвращена Discord-роль в течение 7 дней",
            )
        sync_profile_name(user, username, guild_nickname)
        if is_owner:
            user.is_admin = user.is_senior_admin = True
    db.flush()
    return user


# === OAuth2 Discord ===
@app.get("/login")
async def login(request: Request):
    redirect_uri = get_discord_redirect_uri(request)
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        raise HTTPException(503, "Вход через Discord не настроен")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_started_at"] = time.time()
    url = f"{DISCORD_API_BASE.rstrip('/')}/api/oauth2/authorize?" + urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
        }
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("oauth_state", None)
    started_at = request.session.pop("oauth_started_at", 0)
    if (
        not expected
        or not state
        or not secrets.compare_digest(state, expected)
        or time.time() - started_at > 600
    ):
        raise HTTPException(
            400, "Сессия авторизации истекла или некорректна. Войдите заново"
        )
    if not code:
        raise HTTPException(400, "Код авторизации не получен")
    redirect_uri = get_discord_redirect_uri(request)

    # Серверные запросы к Discord API — через прокси
    try:
        async with get_discord_client(timeout=30.0) as client:
            # Для form-data используем отдельный Content-Type
            token_url = get_discord_api_url("/oauth2/token")
            token_resp = await client.post(
                token_url,
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                raise HTTPException(
                    400, "Discord отклонил код авторизации. Войдите заново"
                )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(502, "Discord не вернул токен авторизации")

            user_url = get_discord_api_url("/users/@me")
            user_resp = await client.get(
                user_url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if user_resp.status_code != 200:
                raise HTTPException(400, "Ошибка данных пользователя")
            discord_user = user_resp.json()

    except (httpx.RequestError, ValueError):
        raise HTTPException(502, "Discord временно недоступен. Попробуйте войти позже")

    if (
        not isinstance(discord_user, dict)
        or not str(discord_user.get("id", "")).isascii()
        or not str(discord_user.get("id", "")).isdigit()
    ):
        raise HTTPException(502, "Некорректные данные пользователя Discord")
    discord_id = str(discord_user["id"])
    username = discord_user.get("username", "unknown")
    global_name = discord_user.get("global_name") or username
    membership = await check_discord_membership(discord_id)

    if not membership["allowed"]:
        request.session["access_denied_reason"] = membership.get("reason", "unknown")
        request.session["access_denied_message"] = membership.get(
            "message", "Доступ запрещён"
        )
        return RedirectResponse("/access-denied")

    async with transaction(serialize_runtime, restore_runtime) as db:
        purge_expired_users(db)
        guild_nickname = membership.get("guild_nickname") or global_name
        user = ensure_discord_profile(db, discord_id, username, guild_nickname)
        sync_manual(db, user, membership.get("roles"))
        request.session.clear()
        db.flush()
        request.session["user_id"] = user.id
        return RedirectResponse("/")


def link_login_page(request, error="", token="", status_code=200):
    csrf = request.session.setdefault("link_login_csrf", secrets.token_urlsafe(32))
    nonce = secrets.token_urlsafe(24)
    response = templates.TemplateResponse(
        request=request,
        name="link_login.html",
        context={"csrf": csrf, "nonce": nonce, "error": error, "retry_token": token},
        status_code=status_code,
    )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            # HTML form navigation under no-referrer sends Origin: null.
            # Keep the real same-site origin while withholding cross-site Referer.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        }
    )
    return response


@app.get("/auth/link", response_class=HTMLResponse)
async def link_login_get(request: Request):
    # Merely previewing a link never consumes the secret or changes the account.
    return link_login_page(request)


@app.post("/auth/link", response_class=HTMLResponse)
async def link_login_post(
    request: Request, token: str = Form(""), csrf: str = Form("")
):
    expected = request.session.get("link_login_csrf")
    if (
        not expected
        or len(csrf) != 43
        or not csrf.isascii()
        or not secrets.compare_digest(csrf, expected)
    ):
        return link_login_page(
            request, "Откройте ссылку заново и подтвердите вход.", status_code=403
        )
    try:
        async with transaction(serialize_runtime, restore_runtime) as db:
            link = get_valid_login_link(db, token)
            discord_id = link.discord_id
        # Discord calls do not block wallet/game transactions.
        membership = await check_discord_membership(discord_id)
        if not membership["allowed"]:
            raise HTTPException(
                403, membership.get("message", "Для входа нужна Discord-роль")
            )
        async with transaction(serialize_runtime, restore_runtime) as db:
            purge_expired_users(db)
            link = get_valid_login_link(db, token)
            user = ensure_discord_profile(
                db,
                discord_id,
                membership.get("username"),
                membership.get("guild_nickname"),
            )
            link.used_at = link_utcnow()
            sync_manual(db, user, membership.get("roles"))
            # Bind links issued before the first website login to the created account.
            link.user_id = user.id
            user.last_seen = datetime.now()
            log_game(
                db,
                user.id,
                "admin",
                "login_link_used",
                0,
                f"Вход по одноразовой ссылке: {link.source}; инициатор Discord ID {link.issued_by}",
            )
            uid = user.id
        request.session.clear()
        request.session["user_id"] = uid
        return RedirectResponse(
            "/",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except (LoginLinkError, HTTPException) as exc:
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return link_login_page(
            request, message, token if exc.status_code == 503 else "", exc.status_code
        )


def require_link_issuer(request, db):
    actor = (
        db.get(User, request.session.get("user_id"))
        if request.session.get("user_id")
        else None
    )
    if not actor or actor.deleted_at or not actor.is_admin or not is_super_admin(actor):
        raise HTTPException(
            403, "Ссылки для других игроков выдаёт только главный администратор"
        )
    return actor


@app.post("/api/admin/login-links")
async def admin_issue_login_link(request: Request):
    async with transaction(serialize_runtime, restore_runtime) as db:
        require_link_issuer(request, db)
    body = await read_json_object(request)
    try:
        discord_id = validate_discord_id(body.get("discord_id"))
        async with transaction(serialize_runtime, restore_runtime) as db:
            actor = require_link_issuer(request, db)
            if discord_id == ADMIN_DISCORD_ID and not is_owner_admin(actor):
                raise HTTPException(
                    403, "Ссылку для входа владельца может получить только он сам."
                )
        site_origin()
        membership = await check_discord_membership(discord_id)
        if not membership["allowed"]:
            raise HTTPException(
                403, membership.get("message", "У игрока нет требуемой роли")
            )
        if not membership.get("username"):
            raise HTTPException(
                503,
                "Не удалось получить Discord-профиль. Проверьте настройки Discord-сервера",
            )
        async with transaction(serialize_runtime, restore_runtime) as db:
            actor = require_link_issuer(request, db)
            if discord_id == ADMIN_DISCORD_ID and not is_owner_admin(actor):
                raise HTTPException(403, "Вход владельца защищён.")
            _, url = issue_login_link(db, discord_id, "admin", actor.discord_id)
            target = db.query(User).filter(User.discord_id == discord_id).first()
            if target is None:
                log_game(
                    db,
                    actor.id,
                    "admin",
                    "login_link_issued",
                    0,
                    f"Выдана ссылка новому игроку Discord ID {discord_id}",
                )
            return JSONResponse(
                {
                    "url": url,
                    "expires_in": 600,
                    "name": membership.get("guild_nickname") or membership["username"],
                    "new_profile": target is None,
                },
                headers={"Cache-Control": "no-store"},
            )
    except LoginLinkError as exc:
        raise HTTPException(exc.status_code, str(exc))


@app.post("/api/admin/login-links/revoke")
async def admin_revoke_login_links(request: Request, db: Session = Depends(get_db)):
    actor = require_link_issuer(request, db)
    body = await read_json_object(request)
    try:
        discord_id = validate_discord_id(body.get("discord_id"))
    except LoginLinkError as exc:
        raise HTTPException(exc.status_code, str(exc))
    if discord_id == ADMIN_DISCORD_ID and not is_owner_admin(actor):
        raise HTTPException(403, "Вход владельца защищён.")
    count = revoke_login_links(db, discord_id)
    log_game(
        db,
        actor.id,
        "admin",
        "login_link_revoked",
        0,
        f"Отменены ссылки игрока Discord ID {discord_id}: {count}",
    )
    return {"ok": True, "revoked": count}


@app.get("/logout", response_class=HTMLResponse)
async def logout_confirmation(request: Request, user=Depends(get_user)):
    return templates.TemplateResponse(
        request=request, name="logout.html", context={"user": user}
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/access-denied", response_class=HTMLResponse)
async def access_denied(request: Request):
    reason = request.session.get("access_denied_reason", "unknown")
    message = request.session.get("access_denied_message", "Доступ запрещён")
    request.session.pop("access_denied_reason", None)
    request.session.pop("access_denied_message", None)
    return templates.TemplateResponse(
        request=request,
        name="access_denied.html",
        context={
            "reason": reason,
            "message": message,
            "guild_name": GUILD_NAME,
            "role_name": ROLE_NAME,
            "guild_invite_url": GUILD_INVITE_URL,
        },
    )


# === Главная страница ===
@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request, user=Depends(get_user), db: Session = Depends(get_db)
):
    top = (
        db.query(User)
        .filter(User.deleted_at.is_(None))
        .order_by(User.balance.desc())
        .limit(10)
        .all()
    )
    # Передаем ВСЕХ пользователей для таблицы "Все игроки" (JS отсортирует их по алфавиту)
    others = db.query(User).filter(User.deleted_at.is_(None)).all()
    role_filters, user_role_ids, user_role_labels, user_role_ranks = user_role_context(
        db, others
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "top_users": top,
            "other_users": others,
            "total_users": db.query(User).filter(User.deleted_at.is_(None)).count(),
            "role_filters": role_filters,
            "user_role_ids": user_role_ids,
            "user_role_labels": user_role_labels,
            "user_role_ranks": user_role_ranks,
        },
    )


# === Профиль ===
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request, user=Depends(get_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login")
    history = (
        db.query(GameHistory)
        .filter(GameHistory.user_id == user.id)
        .order_by(GameHistory.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "user": user,
            "display_name": user.display_name or user.username,
            "history": history,
            "medals": visible_awards(db, user.discord_id),
            "rule_tests": player_summaries(db, user.id),
        },
    )


@app.post("/profile/update")
async def update_profile(
    new_name: str = Form(None),
    bio: str = Form(None),
    favorite_game: str = Form(None),
    avatar_emoji: str = Form(None),
    quote: str = Form(None),
    theme_color: str = Form(None),
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401)
    if new_name is not None:
        clean = new_name.strip()
        if clean and len(clean) <= 30:
            user.display_name = clean
    if bio is not None:
        user.bio = bio.strip()[:500] if bio.strip() else None
    if favorite_game is not None:
        valid = ["blackjack", "roulette", "poker", "crash", "durak", "billiards", ""]
        user.favorite_game = favorite_game if favorite_game in valid else None
    if avatar_emoji is not None:
        user.avatar_emoji = avatar_emoji.strip()[:10] if avatar_emoji.strip() else None
    if quote is not None:
        user.quote = quote.strip()[:200] if quote.strip() else None
    if theme_color is not None:
        valid = [
            "#667eea",
            "#f093fb",
            "#4facfe",
            "#43e97b",
            "#fa709a",
            "#ffd700",
            "#f44336",
            "#9c27b0",
            "#ff9800",
            "#00bcd4",
            "",
        ]
        user.theme_color = theme_color if theme_color in valid else None
    db.flush()
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/toggle-public")
async def toggle_public(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    user.profile_public = not (
        user.profile_public if user.profile_public is not None else True
    )
    db.flush()
    return {"ok": True, "profile_public": user.profile_public}


@app.get("/api/user/{uid}/public-profile")
async def public_profile(uid: int, request: Request, db: Session = Depends(get_db)):
    target = (
        db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    )
    if not target:
        raise HTTPException(404)
    uid_session = request.session.get("user_id")
    current_user = (
        db.query(User)
        .filter(User.deleted_at.is_(None))
        .filter(User.id == uid_session)
        .first()
        if uid_session
        else None
    )
    is_admin_viewer = current_user and current_user.is_admin
    is_self = current_user and current_user.id == uid
    is_public = target.profile_public if target.profile_public is not None else True
    if not is_public and not is_admin_viewer and not is_self:
        return JSONResponse({"error": "Профиль скрыт"}, status_code=403)
    history = (
        db.query(GameHistory)
        .filter(GameHistory.user_id == uid)
        .order_by(GameHistory.created_at.desc())
        .limit(20)
        .all()
    )
    all_history = db.query(GameHistory).filter(GameHistory.user_id == uid).all()
    wins = sum(
        1
        for h in all_history
        if h.result == "won" and h.game_type not in ("admin", "daily")
    )
    losses = sum(
        1
        for h in all_history
        if h.result == "lost" and h.game_type not in ("admin", "daily")
    )
    pushes = sum(
        1
        for h in all_history
        if h.result == "push" and h.game_type not in ("admin", "daily")
    )
    total_won = sum(
        h.amount
        for h in all_history
        if h.amount > 0 and h.game_type not in ("admin", "daily")
    )
    total_lost = sum(
        h.amount
        for h in all_history
        if h.amount < 0 and h.game_type not in ("admin", "daily")
    )
    game_presence = admin_game_names(db).get(target.id, {})
    site_online = bool(
        target.last_seen and (datetime.now() - target.last_seen).total_seconds() <= 300
    )
    return {
        "rule_tests": player_summaries(db, target.id)
        if (is_self or can_manage_tests(current_user, is_senior_or_super))
        else None,
        "can_review_rule_tests": can_manage_tests(current_user, is_senior_or_super),
        "id": target.id,
        "username": target.username,
        "display_name": target.display_name or target.username,
        "guild_nickname": target.guild_nickname,
        "medals": visible_awards(db, target.discord_id),
        "balance": target.balance,
        "is_admin": target.is_admin,
        "is_public": is_public,
        "is_self": bool(is_self),
        "position": db.query(User)
        .filter(User.deleted_at.is_(None))
        .filter(User.balance > target.balance)
        .count()
        + 1,
        "bio": target.bio,
        "favorite_game": target.favorite_game,
        "avatar_emoji": target.avatar_emoji,
        "quote": target.quote,
        "theme_color": target.theme_color or "#667eea",
        "presence": {
            "site_online": site_online,
            "squad_online": game_presence.get("status") == "online",
            "server_name": game_presence.get("server_name"),
            "game_name": game_presence.get("name"),
        },
        "stats": {
            "total_games": len(
                [h for h in all_history if h.game_type not in ("admin", "daily")]
            ),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "total_won": total_won,
            "total_lost": total_lost,
            "net": total_won + total_lost,
        },
        "history": [
            {
                "game_type": h.game_type,
                "result": h.result,
                "amount": h.amount,
                "financial": is_money_log(h),
                "details": log_details(
                    h,
                    bool(
                        current_user
                        and current_user.is_admin
                        and is_senior_or_super(current_user)
                    ),
                ),
                "time": h.created_at.strftime("%d.%m %H:%M") if h.created_at else "",
            }
            for h in history
        ],
    }


async def read_json_object(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Некорректный JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    for key in ("updates", "user_ids", "steam_ids", "users"):
        if key in body and (not isinstance(body[key], list) or len(body[key]) > 1000):
            raise HTTPException(400, "Ожидается список до 1000 элементов")
    return body


# === АДМИНКА ===
@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    archived = []
    cargo_ids = cargo_user_ids(db)
    cargo_users = (
        db.query(User)
        .filter(User.id.in_(cargo_ids))
        .order_by(User.deleted_at.desc())
        .all()
        if cargo_ids
        else []
    )
    cargo_moved_at = (
        {
            row.user_id: row.cargo200_at
            for row in db.query(RosterMembership)
            .filter(RosterMembership.user_id.in_(cargo_ids))
            .all()
        }
        if cargo_ids
        else {}
    )
    now = datetime.now()
    for u in (
        db.query(User)
        .filter(User.deleted_at.is_not(None))
        .filter(~User.id.in_(cargo_ids))
        .order_by(User.deleted_at.desc())
        .all()
    ):
        deadline = u.deleted_at + timedelta(days=RETENTION_DAYS)
        archived.append(
            {
                "user": u,
                "deadline": deadline,
                "remaining": max(0, int((deadline - now).total_seconds())),
            }
        )
    active_users = db.query(User).filter(User.deleted_at.is_(None)).all()
    role_filters, user_role_ids, user_role_labels, user_role_ranks = user_role_context(
        db, active_users
    )
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "user": admin,
            "users": active_users,
            "admin_discord_id": ADMIN_DISCORD_ID,
            "is_senior_or_super": is_senior_or_super(admin),
            "is_super": is_super_admin(admin),
            "is_owner": bool(is_owner_admin(admin)),
            "archived_users": archived,
            "cargo_users": cargo_users,
            "cargo_moved_at": cargo_moved_at,
            "player_squad": admin_game_names(db),
            "role_filters": role_filters,
            "user_role_ids": user_role_ids,
            "user_role_labels": user_role_labels,
            "user_role_ranks": user_role_ranks,
            "server_rule_questions": list_server_rule_questions(db),
        },
    )


def _rule_question_values(payload):
    try:
        return normalize_question_payload(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/rule-questions")
async def create_rule_question(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    values = _rule_question_values(await read_json_object(request))
    last_position = db.query(func.max(ServerRuleQuestion.position)).scalar() or 0
    row = ServerRuleQuestion(
        position=last_position + 1,
        created_at=rules_utcnow(),
        updated_at=rules_utcnow(),
    )
    apply_question(row, values, admin.discord_id)
    db.add(row)
    db.flush()
    from admin_hub import log_action

    log_action(
        db,
        admin,
        "rule_question_create",
        f"Добавлен вопрос #{row.id}: {row.question_text}",
    )
    return {"ok": True, "question": question_record(row)}


@app.post("/api/admin/rule-questions/{question_id}")
async def update_rule_question(
    question_id: int,
    request: Request,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.get(ServerRuleQuestion, question_id)
    if row is None:
        raise HTTPException(404, "Вопрос не найден")
    values = _rule_question_values(await read_json_object(request))
    apply_question(row, values, admin.discord_id)
    from admin_hub import log_action

    log_action(
        db,
        admin,
        "rule_question_update",
        f"Изменён вопрос #{row.id}: {row.question_text}",
    )
    db.flush()
    return {"ok": True, "question": question_record(row)}


@app.get("/appeals", response_class=HTMLResponse)
async def appeals_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login?next=/appeals", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="appeals.html",
        context={"user": user},
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/admin/player-details")
async def admin_player_details(request: Request):
    from database import SessionLocal

    # Polling names must never settle games, change balances, or hold the wallet lock.
    with SessionLocal() as db:
        uid = request.session.get("user_id")
        admin = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
            if uid
            else None
        )
        if not admin or not admin.is_admin:
            raise HTTPException(403, "Доступ только для администраторов")
        names = admin_game_names(db)
        return JSONResponse(
            {
                "users": [
                    {
                        "id": u.id,
                        "steam_id": u.steam_id,
                        "registered_at": u.registered_at.isoformat() + "Z"
                        if u.registered_at
                        else None,
                        "squad": names[u.id],
                    }
                    for u in db.query(User).filter(User.deleted_at.is_(None)).all()
                ]
            },
            headers={"Cache-Control": "private, no-store"},
        )


@app.get("/api/admin/player-squad-history/{uid}")
def admin_player_squad_history(uid: int, request: Request):
    """BattleMetrics-derived Squad sessions, visible to every active admin."""
    from database import SessionLocal

    with SessionLocal() as db:
        admin_id = request.session.get("user_id")
        admin = (
            db.query(User)
            .filter(User.id == admin_id, User.deleted_at.is_(None))
            .first()
            if admin_id
            else None
        )
        if not admin or not admin.is_admin:
            raise HTTPException(403, "Доступ только для администраторов")
        player = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
        )
        if not player:
            raise HTTPException(404, "Игрок не найден")
        return JSONResponse(
            profile_history(db, player.steam_id),
            headers={"Cache-Control": "private, no-store"},
        )


@app.post("/api/admin/player-squad-history/{uid}/refresh")
def admin_refresh_player_squad_history(uid: int, request: Request):
    from sqlalchemy import text as sql_text

    with SessionLocal.begin() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(sql_text("BEGIN IMMEDIATE"))
        actor = (
            db.get(User, request.session.get("user_id"))
            if request.session.get("user_id")
            else None
        )
        if not actor or not actor.is_admin or actor.deleted_at:
            raise HTTPException(403, "Доступ только для администраторов")
        player = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
        )
        if not player:
            raise HTTPException(404, "Игрок не найден")
        return JSONResponse(
            queue_history_refresh(db, player.steam_id),
            headers={"Cache-Control": "private, no-store"},
        )


@app.get("/api/admin/player-voice-history/{uid}")
async def admin_player_voice_history(uid: int, request: Request):
    """Durable Discord voice events, visible to every active administrator."""
    from database import SessionLocal

    with SessionLocal() as db:
        admin_id = request.session.get("user_id")
        admin = (
            db.query(User)
            .filter(User.id == admin_id, User.deleted_at.is_(None))
            .first()
            if admin_id
            else None
        )
        if not admin or not admin.is_admin:
            raise HTTPException(403, "Доступ только для администраторов")
        player = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
        )
        if not player:
            raise HTTPException(404, "Игрок не найден")
        rows = (
            db.query(DiscordEventLog)
            .filter(
                DiscordEventLog.target_id == str(player.discord_id),
                DiscordEventLog.kind.in_(("voice_join", "voice_leave", "voice_move")),
            )
            .order_by(DiscordEventLog.occurred_at.desc())
            .limit(200)
            .all()
        )
        actor_ids = {row.actor_id for row in rows if row.actor_id}
        actors = (
            {
                str(user.discord_id): (user.display_name or user.username)
                for user in db.query(User).filter(User.discord_id.in_(actor_ids)).all()
            }
            if actor_ids
            else {}
        )
        channel_ids = {
            value
            for row in rows
            for value in (row.channel_from, row.channel_to)
            if value
        }
        channel_names = (
            {
                row.channel_id: row.name
                for row in db.query(DiscordChannelCatalog)
                .filter(DiscordChannelCatalog.channel_id.in_(channel_ids))
                .all()
            }
            if channel_ids
            else {}
        )
        current = db.get(DiscordVoicePresence, str(player.discord_id))
        events = []
        for row in rows:
            try:
                data = json.loads(row.payload)
            except (TypeError, ValueError):
                data = {}
            events.append(
                {
                    "kind": row.kind,
                    "occurred_at": row.occurred_at.isoformat() + "Z",
                    "channel_from": {
                        "name": data.get("channel_from_name")
                        or channel_names.get(row.channel_from)
                    },
                    "channel_to": {
                        "name": data.get("channel_to_name")
                        or channel_names.get(row.channel_to)
                    },
                    "forced": bool(data.get("forced")),
                    "actor_id": row.actor_id,
                    "actor_name": actors.get(str(row.actor_id))
                    if row.actor_id
                    else None,
                    "pending": row.sent_at is None and row.ignored_at is None,
                }
            )
        return JSONResponse(
            {
                "discord_id": str(player.discord_id),
                "online": current is not None,
                "current": (
                    {
                        "name": current.channel_name,
                        "joined_at": current.joined_at.isoformat() + "Z",
                        "observed_at": current.observed_at.isoformat() + "Z",
                    }
                    if current
                    else None
                ),
                "events": events,
            },
            headers={"Cache-Control": "private, no-store"},
        )


@app.get("/api/admin/online-status")
async def online_status(admin=Depends(require_admin), db: Session = Depends(get_db)):
    threshold = datetime.now() - timedelta(minutes=5)
    online_users = (
        db.query(User)
        .filter(User.deleted_at.is_(None))
        .filter(User.last_seen > threshold)
        .all()
    )
    result = []
    for user in online_users:
        seconds_ago = int((datetime.now() - user.last_seen).total_seconds())
        result.append(
            {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name or user.username,
                "guild_nickname": user.guild_nickname,
                "is_admin": user.is_admin,
                "balance": user.balance,
                "last_seen_ago": seconds_ago,
                "activity": get_user_activity(user.id),
            }
        )
    result.sort(key=lambda x: (0 if x["activity"]["game"] else 1, x["last_seen_ago"]))
    return {"users": result, "total": len(result)}


@app.get("/api/admin/discord-roster")
async def admin_discord_roster(request: Request):
    from database import SessionLocal

    # This Discord request intentionally runs outside the shared game/wallet
    # transaction so a slow Discord response cannot pause games or balances.
    with SessionLocal() as db:
        uid = request.session.get("user_id")
        admin = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
            if uid
            else None
        )
        if not admin or not admin.is_admin:
            raise HTTPException(403, "Доступ только для администраторов")
    role_members = await fetch_required_role_members()
    with SessionLocal() as db:
        site_users = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .order_by(User.id.asc())
            .all()
        )
    by_discord_id = {str(user.discord_id): user for user in site_users}
    missing_site = [
        member for member in role_members if member["discord_id"] not in by_discord_id
    ]
    missing_steam = [
        {
            "id": user.id,
            "discord_id": str(user.discord_id),
            "username": user.username,
            "display_name": user.display_name or user.guild_nickname or user.username,
        }
        for user in site_users
        if not (user.steam_id or "").strip()
    ]
    missing_steam.sort(
        key=lambda row: (str(row["display_name"]).casefold(), row["discord_id"])
    )
    role_name = (
        ROLE_NAME if ROLE_NAME and ROLE_NAME != "определённую роль" else "Участник ST-9"
    )
    return JSONResponse(
        {
            "role_name": role_name,
            "checked_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "counts": {
                "discord_role": len(role_members),
                "missing_site": len(missing_site),
                "missing_steam": len(missing_steam),
            },
            "missing_site": missing_site,
            "missing_steam": missing_steam,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/admin/assembly-status")
async def admin_assembly_status(request: Request):
    from assembly_attendance import (
        AssemblyAssessment,
        AssemblyWindow,
        INTERVAL as ASSEMBLY_INTERVAL,
        MOSCOW as ASSEMBLY_MOSCOW,
        SERVER_IDS as ASSEMBLY_SERVERS,
        bounds as assembly_bounds,
        coverage as assembly_coverage,
    )
    from database import SessionLocal

    def stamp(value):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    def object_json(value, fallback):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (TypeError, ValueError):
            return fallback

    with SessionLocal() as db:
        uid = request.session.get("user_id")
        admin = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
            if uid
            else None
        )
        if not admin or not admin.is_admin:
            raise HTTPException(403, "Доступ только для администраторов")
        windows = (
            db.query(AssemblyWindow)
            .order_by(AssemblyWindow.event_date.desc())
            .limit(12)
            .all()
        )
        dates = [window.event_date for window in windows]
        assessments = (
            db.query(AssemblyAssessment)
            .filter(AssemblyAssessment.event_date.in_(dates))
            .order_by(AssemblyAssessment.event_date.desc(), AssemblyAssessment.id.asc())
            .all()
            if dates
            else []
        )
        user_ids = {row.user_id for row in assessments if row.user_id is not None}
        users = (
            {
                user.id: user
                for user in db.query(User).filter(User.id.in_(user_ids)).all()
            }
            if user_ids
            else {}
        )

        by_date = {event_date: [] for event_date in dates}
        outcome_labels = {
            "excused": "Рапорт принят",
            "present": "Был на сервере",
            "present_voice": "Был в войсе Discord",
            "penalized": "Штраф",
            "appeal_approved": "Апелляция одобрена",
            "unregistered": "Нет профиля на сайте",
            "steam_unlinked": "Не привязан Steam ID",
            "pending_penalty": "Ожидает списания",
            "exempted": "Освобождён администратором",
        }
        outcome_order = {
            "pending_penalty": 0,
            "penalized": 0,
            "appeal_approved": 1,
            "unregistered": 1,
            "steam_unlinked": 2,
            "excused": 3,
            "present": 4,
            "present_voice": 4,
            "exempted": 5,
        }
        for row in assessments:
            user = users.get(row.user_id)
            by_date.setdefault(row.event_date, []).append(
                {
                    "discord_id": row.discord_id,
                    "user_id": row.user_id,
                    "display_name": (
                        (user.display_name or user.guild_nickname or user.username)
                        if user
                        else row.discord_id
                    ),
                    "username": user.username if user else None,
                    "outcome": row.outcome,
                    "outcome_label": outcome_labels.get(row.outcome, row.outcome),
                    "reported_at": stamp(row.reported_at),
                    "attended_at": stamp(row.attended_at),
                    "server_id": row.server_id,
                    "assessed": row.assessed_cents / 100,
                    "charged": row.charged_cents / 100,
                    "balance_before": (
                        row.balance_before_cents / 100
                        if row.balance_before_cents is not None
                        else None
                    ),
                    "balance_after": (
                        row.balance_after_cents / 100
                        if row.balance_after_cents is not None
                        else None
                    ),
                }
            )

        problem_labels = {
            "late_start": "мониторинг начался поздно",
            "early_end": "мониторинг закончился рано",
            "monitor_gap": "был перерыв более трёх минут",
            "too_few_samples": "недостаточно снимков",
            "server_coverage": "не все серверы проверены достаточно часто",
            "discord_not_observed": "нет записей войсов Discord",
            "discord_late_start": "учёт Discord начался поздно",
            "discord_early_end": "учёт Discord закончился рано",
            "discord_monitor_gap": "был перерыв в учёте Discord",
            "discord_too_few_samples": "недостаточно снимков Discord",
            "discord_invalid_coverage": "не удалось проверить записи Discord",
        }
        status_labels = {
            "collecting": "Идёт сбор данных",
            "forum_pending": "Ожидаются данные Discord",
            "source_incomplete": "Недостаточно данных — штрафы отменены",
            "completed": "Проверка завершена",
            "review_pending": "Проверка администратором",
        }
        error_labels = {
            "report_thread_missing": "тема рапортов с датой сбора пока не найдена",
            "report_thread_ambiguous": "найдено несколько тем рапортов с этой датой",
            "invalid_member_list": "Discord вернул некорректный список участников",
            "invalid_member_page": "Discord вернул неполный список участников",
            "invalid_messages": "Discord вернул некорректный список рапортов",
            "invalid_message_page": "Discord вернул неполный список рапортов",
        }
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        result = []
        for window in windows:
            health = object_json(window.healthy_servers, {})
            attended = object_json(window.attended, {})
            members = object_json(window.member_ids, [])
            decisions = by_date.get(window.event_date, [])
            decisions.sort(
                key=lambda item: (
                    outcome_order.get(item["outcome"], 99),
                    str(item["display_name"]).casefold(),
                )
            )
            coverage_ok, problems = assembly_coverage(window)
            collecting_now = window.status == "collecting" and now_utc < window.ends_at
            expected = max(
                1,
                int(
                    (window.ends_at - window.starts_at).total_seconds()
                    // ASSEMBLY_INTERVAL
                ),
            )
            measured = min(
                [window.sample_count or 0]
                + [int(health.get(server_id, 0)) for server_id in ASSEMBLY_SERVERS]
            )
            counts = Counter(item["outcome"] for item in decisions)
            result.append(
                {
                    "event_date": window.event_date,
                    "status": window.status,
                    "status_label": status_labels.get(window.status, window.status),
                    "starts_at": stamp(window.starts_at),
                    "ends_at": stamp(window.ends_at),
                    "processed_at": stamp(window.processed_at),
                    "review_until": None,
                    "requires_confirmation": window.status == "review_pending",
                    "retry_at": stamp(window.retry_at),
                    "attempts": window.attempts or 0,
                    "thread_id": window.thread_id,
                    "last_error": error_labels.get(
                        window.last_error, window.last_error
                    ),
                    "coverage_ok": None if collecting_now else coverage_ok,
                    "coverage_percent": min(
                        100,
                        max(
                            round(measured * 100 / expected),
                            round(
                                int((health.get("discord_voice") or {}).get("count", 0))
                                * 100
                                / max(
                                    1,
                                    int(
                                        (
                                            window.ends_at - window.starts_at
                                        ).total_seconds()
                                        // 60
                                    ),
                                )
                            ),
                        ),
                    ),
                    "coverage_problems": []
                    if collecting_now
                    else [problem_labels.get(problem, problem) for problem in problems],
                    "sample_count": window.sample_count or 0,
                    "max_gap_seconds": window.max_gap_seconds or 0,
                    "member_count": len(members),
                    "attendance_count": len(attended),
                    "assessment_count": len(decisions),
                    "report_count": sum(
                        1 for item in decisions if item["reported_at"] is not None
                    ),
                    "present_count": sum(
                        1 for item in decisions if item["attended_at"] is not None
                    ),
                    "penalty_count": counts.get("penalized", 0)
                    + counts.get("pending_penalty", 0),
                    "charged": sum(item["charged"] for item in decisions),
                    "decisions": decisions,
                }
            )

    local_now = datetime.now(timezone.utc).astimezone(ASSEMBLY_MOSCOW)
    days_until = (5 - local_now.weekday()) % 7
    next_day = local_now.date() + timedelta(days=days_until)
    today_start, today_end = assembly_bounds(local_now.date())
    if days_until == 0 and datetime.now(timezone.utc).replace(tzinfo=None) >= today_end:
        next_day += timedelta(days=7)
    next_start_raw, next_end_raw = assembly_bounds(next_day)
    next_start = next_start_raw.replace(tzinfo=timezone.utc)
    next_end = next_end_raw.replace(tzinfo=timezone.utc)
    return JSONResponse(
        {
            "next_start": next_start.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "next_end": next_end.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "windows": result,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/admin/balance/{uid}")
async def set_balance(
    uid: int,
    balance: float = Form(...),
    reason: str = Form(""),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not is_finite_number(balance) or not MIN_BALANCE <= balance <= MAX_BALANCE:
        raise HTTPException(
            400, "Баланс должен быть конечным числом в допустимом диапазоне"
        )
    u = db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    if not u:
        raise HTTPException(404)
    protect_owner(admin, u)
    admin_name = admin.display_name or admin.username
    reason_clean = reason.strip() if reason else ""
    if len(reason_clean) > 200:
        raise HTTPException(400, "Причина должна содержать не более 200 символов")
    if not is_senior_or_super(admin) and not reason_clean:
        raise HTTPException(
            400, "Младшие админы обязаны указывать причину изменения баланса"
        )
    diff = balance - u.balance
    u.balance = balance
    if reason_clean:
        details = f"{'📈' if diff > 0 else '📉'} Админ {admin_name}: {u.balance - diff:.2f} → {balance:.2f} | Причина: {reason_clean}"
    else:
        details = f"{'📈' if diff > 0 else '📉'} Админ {admin_name}: {u.balance - diff:.2f} → {balance:.2f}"
    log_game(db, uid, "admin", "admin_adjustment", diff, details)
    db.flush()
    return {
        "ok": True,
        "message": f"Баланс изменён на {balance:.2f}",
        "new_balance": u.balance,
    }


@app.post("/admin/name/{uid}")
async def set_name(
    uid: int,
    new_name: str = Form(...),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    if not u:
        raise HTTPException(404)
    protect_owner(admin, u)
    clean = new_name.strip()
    if not clean or len(clean) > 30:
        raise HTTPException(400)
    old = u.display_name or u.username
    u.display_name = clean
    log_game(
        db,
        uid,
        "admin",
        "name_change",
        0,
        f"✏️ {admin.display_name or admin.username}: {old} → {clean}",
    )
    db.flush()
    return {"ok": True, "message": f"Ник изменён на {clean}"}


@app.post("/admin/notes/{uid}")
async def save_admin_notes(
    uid: int,
    note: str = Form(""),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Заметка админа — сохраняется БЕЗ логирования в историю"""
    if not is_senior_or_super(admin):
        raise HTTPException(403, "Только старшие админы могут писать заметки")
    u = db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    if not u:
        raise HTTPException(404)
    clean_note = note.strip()[:1000] if note else ""
    u.admin_notes = clean_note if clean_note else None
    db.flush()
    return {"ok": True, "message": "Заметка сохранена"}


@app.get("/admin/logs", response_class=HTMLResponse)
async def admin_all_logs(
    request: Request,
    q: str = "",
    game: str = "",
    outcome: str = "",
    date_from: str = "",
    date_to: str = "",
    before: int | None = None,
    player_id: str = "",
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        selected_player = int(player_id) if player_id else None
        if selected_player is not None and selected_player <= 0:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Неверный ID игрока")
    context = audit_page_context(
        db,
        q=q,
        game=game,
        outcome=outcome,
        date_from=date_from,
        date_to=date_to,
        before=before,
        player_id=selected_player,
        show_private=bool(is_senior_or_super(admin)),
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_logs.html",
        context={
            **context,
            "user": admin,
            "is_senior_or_super": is_senior_or_super(admin),
            "admin_discord_id": ADMIN_DISCORD_ID,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/admin/logs/{uid}", response_class=HTMLResponse)
async def admin_user_logs(
    request: Request,
    uid: int,
    q: str = "",
    game: str = "",
    outcome: str = "",
    date_from: str = "",
    date_to: str = "",
    before: int | None = None,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    context = audit_page_context(
        db,
        uid=uid,
        q=q,
        game=game,
        outcome=outcome,
        date_from=date_from,
        date_to=date_to,
        before=before,
        show_private=bool(is_senior_or_super(admin)),
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_logs.html",
        context={
            **context,
            "user": admin,
            "is_senior_or_super": is_senior_or_super(admin),
            "admin_discord_id": ADMIN_DISCORD_ID,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/admin/delete/{uid}")
async def delete_user(
    uid: int, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    if not is_senior_or_super(admin):
        raise HTTPException(403, "Только старшие админы могут удалять игроков")
    if uid == admin.id:
        raise HTTPException(400, "Нельзя удалить себя")
    target = (
        db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    )
    if not target:
        raise HTTPException(404)
    if target.discord_id == ADMIN_DISCORD_ID:
        raise HTTPException(403, "Нельзя удалить главного админа")
    if target.is_chief_admin and not is_super_admin(admin):
        raise HTTPException(
            403,
            "Удалить главного администратора может только другой главный администратор.",
        )
    detach_user_from_games(db, target)
    current_runtime.get().next_user_id = max(
        current_runtime.get().next_user_id, target.id + 1
    )
    log_game(
        db,
        uid,
        "admin",
        "user_deleted",
        0,
        f"🗑 Удалён админом {admin.display_name or admin.username}",
    )
    revoke_login_links(db, target.discord_id)
    anonymize_player(db, target.id)
    db.delete(target)
    db.flush()
    return {"ok": True, "message": "Пользователь удалён"}


@app.post("/admin/toggle-admin/{uid}")
async def toggle_admin(
    uid: int,
    action: str = Form("toggle"),
    super_admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not is_super_admin(super_admin):
        raise HTTPException(403, "Только супер-админ может управлять правами")
    target = (
        db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    )
    if not target:
        raise HTTPException(404)
    if target.id == super_admin.id or target.discord_id == ADMIN_DISCORD_ID:
        raise HTTPException(400, "Нельзя изменить права")
    admin_name = super_admin.display_name or super_admin.username
    if action == "make_chief":
        target.is_admin = target.is_senior_admin = target.is_chief_admin = True
        msg = (
            f"👑 {target.display_name or target.username} теперь главный администратор"
        )
        details = f"👑 Главный админ {admin_name} назначил главным"
    elif action == "make_senior":
        target.is_chief_admin = False
        target.is_admin = True
        target.is_senior_admin = True
        msg = (
            f"⭐ {target.display_name or target.username} теперь старший администратор"
        )
        details = f"⭐ Супер-админ {admin_name} сделал старшим"
    elif action == "make_junior":
        target.is_chief_admin = False
        target.is_admin = True
        target.is_senior_admin = False
        msg = f"⚙️ {target.display_name or target.username} теперь младший администратор"
        details = f"⚙️ Супер-админ {admin_name} сделал младшим"
    elif action == "remove":
        target.is_chief_admin = False
        target.is_admin = False
        target.is_senior_admin = False
        msg = f"⬇️ {target.display_name or target.username} больше не администратор"
        details = f"⬇️ Супер-админ {admin_name} снял права"
    else:
        raise HTTPException(400, "Неверное действие")
    log_game(db, uid, "admin", "role_change", 0, details)
    db.flush()
    return {
        "ok": True,
        "is_admin": target.is_admin,
        "is_senior_admin": target.is_senior_admin,
        "is_chief_admin": target.is_chief_admin,
        "message": msg,
    }


@app.post("/admin/batch-update")
@bulk_preview
async def batch_update_balances(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    if not is_senior_or_super(admin):
        raise HTTPException(
            403, "Массовые изменения доступны только старшим администраторам"
        )
    body = await read_json_object(request)
    updates = body.get("updates", [])
    admin_name = admin.display_name or admin.username
    results = []
    for update in updates:
        if not isinstance(update, dict):
            raise HTTPException(400, "Неверная запись обновления")
        uid = update.get("user_id")
        if type(uid) is not int:
            raise HTTPException(400, "Неверный ID пользователя")
        new_balance = update.get("balance")
        if (
            uid is None
            or new_balance is None
            or not is_finite_number(new_balance)
            or not MIN_BALANCE <= float(new_balance) <= MAX_BALANCE
        ):
            continue
        new_balance = float(new_balance)
        u = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.id == uid)
            .first()
        )
        if not u:
            continue
        protect_owner(admin, u)
        old = u.balance
        diff = new_balance - old
        if abs(diff) < 0.01:
            continue
        u.balance = new_balance
        log_game(
            db,
            uid,
            "admin",
            "admin_adjustment",
            diff,
            f"{'📈' if diff > 0 else '📉'} {admin_name}",
        )
        results.append(
            {
                "user_id": uid,
                "name": u.display_name or u.username,
                "old": old,
                "new": new_balance,
            }
        )
    db.flush()
    return {"ok": True, "message": f"Обновлено {len(results)}", "updated": results}


@app.post("/admin/mass-adjust")
@bulk_preview
async def mass_adjust_balances(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    if not is_senior_or_super(admin):
        raise HTTPException(
            403, "Массовые изменения доступны только старшим администраторам"
        )
    body = await read_json_object(request)
    user_ids = body.get("user_ids", [])
    amount = body.get("amount", 0)
    if not is_finite_number(amount) or abs(float(amount)) > MAX_BALANCE:
        raise HTTPException(400, "Неверная сумма")
    amount = float(amount)
    if not user_ids or amount == 0:
        raise HTTPException(400, "Неверные параметры")
    admin_name = admin.display_name or admin.username
    results = []
    if any(type(uid) is not int for uid in user_ids):
        raise HTTPException(400, "Неверный ID пользователя")
    for uid in dict.fromkeys(user_ids):
        u = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.id == uid)
            .first()
        )
        if not u:
            continue
        protect_owner(admin, u)
        old = u.balance
        if not MIN_BALANCE <= old + amount <= MAX_BALANCE:
            continue
        u.balance = old + amount
        log_game(
            db,
            uid,
            "admin",
            "admin_adjustment",
            amount,
            f"{'📈' if amount > 0 else '📉'} Массовое от {admin_name}",
        )
        results.append(
            {
                "user_id": uid,
                "name": u.display_name or u.username,
                "old": old,
                "new": u.balance,
            }
        )
    db.flush()
    return {
        "ok": True,
        "message": f"{'Начислено' if amount > 0 else 'Списано'} {abs(amount):.2f}",
        "updated": results,
    }


@app.get("/admin/export")
async def export_balances(admin=Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).filter(User.deleted_at.is_(None)).all()
    return {
        "users": [
            {
                "discord_id": u.discord_id,
                "username": u.username,
                "display_name": u.display_name,
                "guild_nickname": u.guild_nickname,
                "balance": u.balance,
                "is_admin": u.is_admin,
                "steam_id": u.steam_id,
                "profile_public": u.profile_public,
                "admin_notes": u.admin_notes if is_senior_or_super(admin) else None,
                "bio": u.bio,
                "favorite_game": u.favorite_game,
                "avatar_emoji": u.avatar_emoji,
                "quote": u.quote,
                "theme_color": u.theme_color,
                "daily_streak": u.daily_streak,
            }
            for u in users
        ]
    }


@app.post("/admin/import")
@bulk_preview
async def import_balances(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    if not is_senior_or_super(admin):
        raise HTTPException(
            403, "Массовые изменения доступны только старшим администраторам"
        )
    body = await read_json_object(request)
    users_data = body.get("users", [])
    count = 0
    if not isinstance(users_data, list):
        raise HTTPException(400)
    for ud in users_data:
        if not isinstance(ud, dict):
            continue
        discord_id = str(ud.get("discord_id", "")).strip()
        if not discord_id or "balance" not in ud:
            continue
        try:
            balance = float(ud["balance"])
        except (TypeError, ValueError, OverflowError):
            continue
        if not is_finite_number(balance) or not MIN_BALANCE <= balance <= MAX_BALANCE:
            continue
        user = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.discord_id == discord_id)
            .first()
        )
        if user:
            protect_owner(admin, user)
            old_balance = user.balance
            user.balance = balance
            if "steam_id" in ud:
                steam_id = ud["steam_id"]
                if steam_id is not None and not isinstance(steam_id, str):
                    raise HTTPException(400, "Неверный Steam ID в импорте")
                clean_steam = (steam_id or "").strip()
                if clean_steam and not validate_steam_id(clean_steam):
                    raise HTTPException(400, "Неверный Steam ID в импорте")
                user.steam_id = clean_steam or None
            if "profile_public" in ud:
                if type(ud["profile_public"]) is not bool:
                    raise HTTPException(400, "Неверная настройка приватности в импорте")
                user.profile_public = ud["profile_public"]
            log_game(
                db,
                user.id,
                "admin",
                "admin_adjustment",
                balance - old_balance,
                "Импорт баланса администратором",
            )

            # Обновляем отображаемое имя
            display_name = ud.get("display_name")
            if isinstance(display_name, str) and display_name.strip():
                user.display_name = display_name.strip()[:30]

            # Обновляем заметки админа
            admin_notes = ud.get("admin_notes")
            if isinstance(admin_notes, str):
                user.admin_notes = (
                    admin_notes.strip()[:1000] if admin_notes.strip() else None
                )

            # Обновляем профиль
            bio = ud.get("bio")
            if isinstance(bio, str):
                user.bio = bio.strip()[:500] if bio.strip() else None

            fav_game = ud.get("favorite_game")
            if isinstance(fav_game, str) and fav_game in [
                "blackjack",
                "roulette",
                "poker",
                "crash",
                "",
            ]:
                user.favorite_game = fav_game if fav_game else None

            avatar = ud.get("avatar_emoji")
            if isinstance(avatar, str):
                user.avatar_emoji = avatar.strip()[:10] if avatar.strip() else None

            quote = ud.get("quote")
            if isinstance(quote, str):
                user.quote = quote.strip()[:200] if quote.strip() else None

            theme = ud.get("theme_color")
            if isinstance(theme, str):
                allowed_colors = {
                    "#667eea",
                    "#f093fb",
                    "#4facfe",
                    "#43e97b",
                    "#fa709a",
                    "#ffd700",
                    "#f44336",
                    "#9c27b0",
                    "#ff9800",
                    "#00bcd4",
                }
                user.theme_color = theme if theme in allowed_colors else None

            streak = ud.get("daily_streak")
            if isinstance(streak, int) and streak >= 0:
                user.daily_streak = streak

            count += 1
    db.flush()
    return {"ok": True, "message": f"Импортировано {count}"}


# === Steam ID ===
def validate_steam_id(steam_id: str) -> bool:
    if not steam_id:
        return False
    clean = steam_id.strip()
    if not clean.isascii() or not clean.isdigit():
        return False
    if len(clean) != 17:
        return False
    if not clean.startswith("7656"):
        return False
    return True


@app.post("/admin/set-steam/{uid}")
async def set_user_steam(
    uid: int,
    steam_id: str = Form(""),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.deleted_at.is_(None)).filter(User.id == uid).first()
    if not u:
        raise HTTPException(404)
    clean = steam_id.strip()
    if clean and not validate_steam_id(clean):
        raise HTTPException(
            400, "Неверный Steam ID (должен быть 17 цифр, начинается с 7656)"
        )
    if clean:
        existing = db.query(User).filter(User.steam_id == clean, User.id != uid).first()
        if existing:
            raise HTTPException(
                400, "Этот Steam ID уже привязан к другому пользователю"
            )
    u.steam_id = clean if clean else None
    admin_name = admin.display_name or admin.username
    if clean:
        log_game(
            db,
            uid,
            "admin",
            "steam_set",
            0,
            f"🎮 Steam ID: {clean} (админ {admin_name})",
        )
    else:
        log_game(
            db,
            uid,
            "admin",
            "steam_cleared",
            0,
            f"🗑 Steam ID удалён (админ {admin_name})",
        )
    db.flush()
    return {"ok": True, "message": "Steam ID сохранён"}


@app.post("/admin/mass-adjust-steam")
@bulk_preview
async def mass_adjust_by_steam(
    request: Request, admin=Depends(require_admin), db: Session = Depends(get_db)
):
    if not is_senior_or_super(admin):
        raise HTTPException(
            403, "Массовые изменения доступны только старшим администраторам"
        )
    body = await read_json_object(request)
    steam_ids_raw = body.get("steam_ids", [])
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "Неверная сумма")
    if not is_finite_number(amount) or abs(float(amount)) > MAX_BALANCE:
        raise HTTPException(400, "Неверная сумма")
    if not steam_ids_raw:
        raise HTTPException(400, "Список Steam ID пуст")
    if amount == 0:
        raise HTTPException(400, "Сумма не может быть 0")
    valid_ids = []
    invalid_ids = []
    for sid in steam_ids_raw:
        sid_clean = str(sid).strip()
        if validate_steam_id(sid_clean):
            valid_ids.append(sid_clean)
        else:
            invalid_ids.append(sid_clean)
    if not valid_ids:
        raise HTTPException(400, "Нет валидных Steam ID в списке")
    admin_name = admin.display_name or admin.username
    results = []
    not_found = []
    for sid in dict.fromkeys(valid_ids):
        u = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.steam_id == sid)
            .first()
        )
        if not u:
            not_found.append(sid)
            continue
        protect_owner(admin, u)
        old = u.balance
        if not MIN_BALANCE <= old + amount <= MAX_BALANCE:
            not_found.append(sid)
            continue
        u.balance = old + amount
        log_game(
            db,
            u.id,
            "admin",
            "admin_adjustment",
            amount,
            f"{'📈' if amount > 0 else '📉'} Массовое по Steam ID от {admin_name}",
        )
        results.append(
            {
                "steam_id": sid,
                "user_id": u.id,
                "name": u.display_name or u.username,
                "old": old,
                "new": u.balance,
            }
        )
    db.flush()
    action = "Начислено" if amount > 0 else "Списано"
    return {
        "ok": True,
        "message": f"{action} {abs(amount):.2f} для {len(results)} пользователей",
        "updated": results,
        "not_found": not_found,
        "invalid": invalid_ids,
    }


@app.post("/api/steam/bind")
async def bind_steam_self(
    steam_id: str = Form(...), user=Depends(get_user), db: Session = Depends(get_db)
):
    if not user:
        raise HTTPException(401)
    clean = steam_id.strip()
    if not validate_steam_id(clean):
        raise HTTPException(
            400, "Неверный Steam ID. Используйте SteamID64 (17 цифр, начинается с 7656)"
        )
    existing = db.query(User).filter(User.steam_id == clean, User.id != user.id).first()
    if existing:
        raise HTTPException(400, "Этот Steam ID уже привязан к другому пользователю")
    user.steam_id = clean
    log_game(db, user.id, "admin", "steam_set", 0, f"🎮 Привязал Steam ID: {clean}")
    db.flush()
    return {"ok": True, "message": "Steam ID привязан"}


# === Баг-репорты через Cloudflare Worker ===
async def get_reporter(request: Request):
    from types import SimpleNamespace

    async with transaction(serialize_runtime, restore_runtime) as db:
        uid = request.session.get("user_id")
        user = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
            if uid
            else None
        )
        if user is None:
            raise HTTPException(401, "Не авторизован")
        return SimpleNamespace(
            **{
                name: getattr(user, name)
                for name in (
                    "id",
                    "discord_id",
                    "username",
                    "display_name",
                    "balance",
                    "is_admin",
                )
            }
        )


@app.post("/api/bug-report")
async def submit_bug_report(
    subject: str = Form(...), message: str = Form(...), user=Depends(get_reporter)
):
    if not user:
        raise HTTPException(401, "Не авторизован")
    if not DISCORD_BUGS_WEBHOOK:
        raise HTTPException(500, "Webhook не настроен администратором")

    subject_clean = subject.strip()[:100]
    message_clean = message.strip()[:2000]

    if not subject_clean:
        raise HTTPException(400, "Заполните тему")
    if not message_clean or len(message_clean) < 10:
        raise HTTPException(400, "Опишите проблему подробнее (минимум 10 символов)")

    payload = {
        "username": "🐛 Bug Reporter",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"🐛 {subject_clean}",
                "description": message_clean,
                "color": 16734000,
                "fields": [
                    {
                        "name": "👤 От кого",
                        "value": f"**{user.display_name or user.username}** (ID: {user.id})",
                        "inline": False,
                    },
                    {
                        "name": "🎮 Discord",
                        "value": f"@{user.username} · `{user.discord_id}`",
                        "inline": False,
                    },
                    {
                        "name": "💰 Баланс",
                        "value": f"{user.balance:.2f}",
                        "inline": True,
                    },
                    {
                        "name": "👑 Админ",
                        "value": "Да" if user.is_admin else "Нет",
                        "inline": True,
                    },
                ],
                "footer": {"text": "BalanceApp Bug Report"},
                "timestamp": datetime.utcnow().isoformat(),
            }
        ],
    }

    headers = {"Content-Type": "application/json"}
    if DISCORD_PROXY_TOKEN:
        headers["X-Proxy-Token"] = DISCORD_PROXY_TOKEN

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    DISCORD_BUGS_WEBHOOK, json=payload, headers=headers
                )
                if resp.status_code in (200, 204):
                    break
                elif resp.status_code == 429:
                    try:
                        retry_after = float(resp.headers.get("retry-after", "2"))
                    except ValueError:
                        retry_after = 2
                    retry_after = (
                        min(10, max(0.1, retry_after))
                        if math.isfinite(retry_after)
                        else 2
                    )
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    raise HTTPException(
                        500, f"Discord вернул ошибку {resp.status_code}"
                    )
        except httpx.RequestError:
            if attempt == 2:
                raise HTTPException(500, "Ошибка сети при отправке")
            await asyncio.sleep(1)
    else:
        raise HTTPException(500, "Не удалось отправить после 3 попыток")

    async with transaction(serialize_runtime, restore_runtime) as db:
        if db.query(User).filter(User.id == user.id, User.deleted_at.is_(None)).first():
            log_game(
                db, user.id, "admin", "bug_report", 0, f"🐛 Баг-репорт: {subject_clean}"
            )
    return {"ok": True, "message": "Сообщение отправлено! Спасибо за обратную связь 🙏"}


# === Авто-удаление без роли (через прокси) ===
@app.post("/admin/check-roles")
async def manual_check_roles(admin=Depends(require_admin)):
    if not is_senior_or_super(admin):
        raise HTTPException(
            403, "Только старшие администраторы могут запускать проверку"
        )
    task = asyncio.create_task(run_role_check_once())
    manual_role_tasks.add(task)
    task.add_done_callback(manual_role_tasks.discard)
    return {"ok": True, "message": "Проверка запущена в фоне"}


manual_role_tasks = set()
role_check_lock = asyncio.Lock()
RETENTION_DAYS = 7


def detach_user_from_games(db, user):
    durak_service.detach(user.id, db)
    billiards_service.detach(user.id, db)
    game = active_games.pop(user.id, None)
    if game and game["status"] in {"playing", "showdown"}:
        user.balance += game["bet"]
        log_game(
            db,
            user.id,
            "blackjack",
            "cancelled",
            0,
            "Возврат ставки при удалении аккаунта",
        )
    crash = crash_games.pop(user.id, None)
    if crash and not crash.settlement_done:
        user.balance += crash.bet
        log_game(
            db, user.id, "crash", "cancelled", 0, "Возврат ставки при удалении аккаунта"
        )
    for tid, table in list(poker_tables.items()):
        if not any(p["user_id"] == user.id for p in table.players):
            continue
        refresh_table_balances(db, table)
        table.remove_player(user.id)
        sync_table_balances(db, table)
        connected = [p for p in table.players if p["connected"]]
        if table.creator_id == user.id and connected:
            table.creator_id = connected[0]["user_id"]
        if not table.players:
            poker_tables.pop(tid, None)
            table_requests.pop(tid, None)
    for tid in table_requests:
        table_requests[tid] = [
            r for r in table_requests[tid] if r["user_id"] != user.id
        ]


def purge_expired_users(db):
    cargo_ids = cargo_user_ids(db)
    expired = (
        db.query(User)
        .filter(User.deleted_at <= datetime.now() - timedelta(days=RETENTION_DAYS))
        .filter(~User.id.in_(cargo_ids))
        .all()
    )
    for user in expired:
        detach_user_from_games(db, user)
        current_runtime.get().next_user_id = max(
            current_runtime.get().next_user_id, user.id + 1
        )
        db.query(GameHistory).filter(GameHistory.user_id == user.id).delete(
            synchronize_session=False
        )
        db.query(LoginLink).filter(LoginLink.discord_id == user.discord_id).delete(
            synchronize_session=False
        )
        notification = db.get(DiscordSyncPending, user.discord_id)
        if notification:
            db.delete(notification)
        anonymize_player(db, user.id)
        db.delete(user)
    db.flush()


def apply_discord_membership(db, user, membership):
    sync_profile_name(
        user, membership.get("username"), membership.get("guild_nickname")
    )
    if membership.get("allowed") and user.deleted_at is None:
        sync_manual(db, user, membership.get("roles"))
    # Keep current seats up to date; historical game/chat records keep their names.
    for table in poker_tables.values():
        for player in table.players:
            if player["user_id"] == user.id:
                player["username"] = user.username
                player["display_name"] = user.display_name or user.username
    for requests in table_requests.values():
        for request in requests:
            if request["user_id"] == user.id:
                request["username"] = user.username
                request["display_name"] = user.display_name or user.username
    for table in durak_service.tables.values():
        durak_service.refresh(table, db)
    if user.discord_id == ADMIN_DISCORD_ID:
        return  # Owner's name syncs too; role removal cannot lock out the owner.
    if not membership["allowed"] and user.deleted_at is None:
        detach_user_from_games(db, user)
        revoke_login_links(db, user.discord_id)
        user.deleted_at = datetime.now()
        cargo = membership.get("reason") == "cargo200" or is_cargo_user(db, user)
        log_game(
            db,
            user.id,
            "admin",
            "user_archived",
            0,
            "Назначена роль «Груз 200»; профиль сохранён без права операций"
            if cargo
            else "Утрачена Discord-роль; восстановление доступно 7 дней",
        )
    elif membership["allowed"] and user.deleted_at is not None:
        if not is_cargo_user(db, user) and datetime.now() - user.deleted_at < timedelta(
            days=RETENTION_DAYS
        ):
            user.deleted_at = None
            log_game(
                db, user.id, "admin", "user_restored", 0, "Discord-роль возвращена"
            )


async def process_pending_discord_sync():
    if not REQUIRED_GUILD_ID or not DISCORD_BOT_TOKEN:
        return
    async with metadata_transaction() as db:
        pending = [
            (r.discord_id, r.version)
            for r in db.query(DiscordSyncPending)
            .filter(DiscordSyncPending.retry_at <= datetime.now())
            .order_by(DiscordSyncPending.retry_at)
            .limit(20)
            .all()
        ]
    for discord_id, version in pending:
        try:
            membership = await check_discord_membership(discord_id)
            async with transaction(serialize_runtime, restore_runtime) as db:
                row = db.get(DiscordSyncPending, discord_id)
                if row is None or row.version != version:
                    continue  # A newer gateway event arrived while REST was in flight.
                user = db.query(User).filter(User.discord_id == discord_id).first()
                if user:
                    if (
                        user.deleted_at
                        and not is_cargo_user(db, user)
                        and datetime.now() - user.deleted_at
                        >= timedelta(days=RETENTION_DAYS)
                    ):
                        purge_expired_users(db)
                    else:
                        apply_discord_membership(db, user, membership)
                        # Keep the latest event timestamp even after verification,
                        # so an older periodic response cannot overwrite this result.
                        row.retry_at = None
                else:
                    db.delete(row)
        except Exception:
            logger.warning(
                "Discord event verification deferred; periodic check will retry"
            )
            async with metadata_transaction() as db:
                row = db.get(DiscordSyncPending, discord_id)
                if row and row.version == version:
                    row.retry_at = datetime.now() + timedelta(seconds=30)
        await asyncio.sleep(0.15)


async def discord_event_checker():
    while True:
        try:
            await process_pending_discord_sync()
        except Exception:
            logger.exception("Discord notification processing failed")
        await asyncio.sleep(1)


async def run_role_check_once():
    if role_check_lock.locked():
        return
    async with role_check_lock:
        async with transaction(serialize_runtime, restore_runtime) as db:
            purge_expired_users(db)
            users = [(u.id, u.discord_id) for u in db.query(User).all()]
        if not REQUIRED_GUILD_ID or not DISCORD_BOT_TOKEN:
            return
        for uid, discord_id in users:
            try:
                # Network calls are outside the database lock.
                started = datetime.now()
                membership = await check_discord_membership(discord_id)
                async with transaction(serialize_runtime, restore_runtime) as db:
                    user = db.query(User).filter(User.id == uid).first()
                    event = db.get(DiscordSyncPending, discord_id)
                    if not user or (event and event.updated_at > started):
                        continue
                    apply_discord_membership(db, user, membership)
            except HTTPException:
                logger.warning("Role verification skipped: Discord unavailable")
            except Exception:
                logger.exception("Role checker failed")
            await asyncio.sleep(0.15)


async def role_checker_task():
    await asyncio.sleep(5)
    while True:
        try:
            await run_role_check_once()
        except Exception:
            logger.exception("Scheduled role check failed")
        await asyncio.sleep(60)


# === ХАБ ИГР ===
@app.get("/games", response_class=HTMLResponse)
async def games_hub(request: Request, user=Depends(get_user)):
    return templates.TemplateResponse(
        request=request, name="games.html", context={"user": user}
    )


# === БЛЭКДЖЕК ===
def create_deck():
    suits = ["♠", "♥", "♦", "♣"]
    values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    return [{"value": v, "suit": s} for s in suits for v in values]


def calculate_score(hand):
    score = 0
    aces = 0
    vals = {
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "6": 6,
        "7": 7,
        "8": 8,
        "9": 9,
        "10": 10,
        "J": 10,
        "Q": 10,
        "K": 10,
        "A": 11,
    }
    for card in hand:
        score += vals[card["value"]]
        if card["value"] == "A":
            aces += 1
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score


async def _resolve_stand(user, game, db):
    while calculate_score(game["dealer"]) < 17:
        game["dealer"].append(game["deck"].pop())
    p = calculate_score(game["player"])
    d = calculate_score(game["dealer"])
    if d > 21 or p > d:
        outcome = "won"
    elif p == d:
        outcome = "push"
    else:
        outcome = "lost"
    if p == 21 and len(game["player"]) == 2:
        if d == 21 and len(game["dealer"]) == 2:
            outcome = "push"
        else:
            outcome = "won"
    elif d == 21 and len(game["dealer"]) == 2:
        outcome = "lost"
    game.update(
        status="showdown",
        outcome=outcome,
        p_score=p,
        d_score=d,
        resolved_at=time.time(),
    )
    return {"ok": True}


def _finalize_blackjack(user, game, db):
    outcome = game.get("outcome")
    bet = game["bet"]
    if outcome == "won":
        win = money(
            Decimal(str(bet))
            * Decimal(
                "2.5" if len(game["player"]) == 2 and game["p_score"] == 21 else "2"
            )
        )
        user.balance += win
        msg = f"Победа! +{win:.2f}"
        log_game(
            db,
            user.id,
            "blackjack",
            "won",
            win - bet,
            f"{game['p_score']} vs {game['d_score']}",
        )
    elif outcome == "push":
        user.balance += bet
        msg = "Ничья."
        log_game(
            db,
            user.id,
            "blackjack",
            "push",
            0,
            f"{game['p_score']} vs {game['d_score']}",
        )
    else:
        msg = f"Дилер выиграл ({game['d_score']} vs {game['p_score']})"
        log_game(
            db,
            user.id,
            "blackjack",
            "lost",
            -bet,
            f"{game['p_score']} vs {game['d_score']}",
        )
    game["status"] = outcome
    game["message"] = msg
    db.flush()


@app.get("/games/blackjack", response_class=HTMLResponse)
async def blackjack_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="games_blackjack.html", context={"user": user}
    )


@app.get("/api/blackjack/state")
async def bj_state(user=Depends(get_user)):
    game = active_games.get(user.id) if user else None
    if not game:
        return {"active": False}
    dealer_visible = (
        [game["dealer"][0]] if game["status"] == "playing" else game["dealer"]
    )
    return {
        "active": True,
        "player": game["player"],
        "dealer": dealer_visible,
        "p_score": calculate_score(game["player"]),
        "d_score": calculate_score(dealer_visible),
        "bet": game["bet"],
        "game_id": game.get("game_id"),
        "status": game["status"],
        "message": game.get("message", ""),
        "resolved_at": game.get("resolved_at"),
        "balance": user.balance,
    }


@app.post("/api/blackjack/start")
async def bj_start_api(
    bet: float = Form(...), user=Depends(get_user), db: Session = Depends(get_db)
):
    if not user:
        raise HTTPException(401)
    if not is_cent_amount(bet) or not 0.01 <= bet <= MAX_BALANCE / 100:
        raise HTTPException(400, "Некорректная ставка")
    if not user:
        raise HTTPException(401)
    # Не списываем две ставки при двойном клике/параллельных запросах.
    current = active_games.get(user.id)
    if current and current.get("status") in ("playing", "showdown"):
        raise HTTPException(400, "Сначала завершите текущую игру")
    if user.balance < bet:
        raise HTTPException(400, "Недостаточно средств")
    user.balance -= bet
    deck = create_deck()
    random.shuffle(deck)
    active_games[user.id] = {
        "game_id": secrets.token_hex(16),
        "deck": deck,
        "player": [deck.pop(), deck.pop()],
        "dealer": [deck.pop(), deck.pop()],
        "bet": float(bet),
        "status": "playing",
        "message": "",
    }
    game = active_games[user.id]
    p_score, d_score = calculate_score(game["player"]), calculate_score(game["dealer"])
    if p_score == 21 or d_score == 21:
        outcome = "push" if p_score == d_score else "won" if p_score == 21 else "lost"
        game.update(
            status="showdown",
            outcome=outcome,
            p_score=p_score,
            d_score=d_score,
            resolved_at=time.time(),
        )
    db.flush()
    return {"ok": True, "balance": user.balance}


@app.post("/api/blackjack/hit")
async def bj_hit_api(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    game = active_games.get(user.id)
    if not game or game["status"] != "playing":
        raise HTTPException(400)
    if not game["deck"]:
        raise HTTPException(409, "Колода закончилась")
    game["player"].append(game["deck"].pop())
    score = calculate_score(game["player"])
    if score > 21:
        game["status"] = "showdown"
        game["outcome"] = "lost"
        game["p_score"] = score
        game["d_score"] = calculate_score(game["dealer"])
        game["resolved_at"] = time.time()
    elif score == 21:
        return await _resolve_stand(user, game, db)
    return {"ok": True}


@app.post("/api/blackjack/stand")
async def bj_stand_api(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    game = active_games.get(user.id)
    if not game or game["status"] != "playing":
        raise HTTPException(400)
    return await _resolve_stand(user, game, db)


@app.post("/api/blackjack/finalize")
async def bj_finalize_api(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    game = active_games.get(user.id)
    if game and game["status"] in {"won", "lost", "push"}:
        return {
            "ok": True,
            "status": game["status"],
            "message": game["message"],
            "balance": user.balance,
        }
    if not game or game["status"] != "showdown":
        raise HTTPException(400)
    if time.time() - game.get("resolved_at", 0) < 4:
        return {"wait": 1}
    _finalize_blackjack(user, game, db)
    return {
        "ok": True,
        "status": game["status"],
        "message": game["message"],
        "balance": user.balance,
    }


# === РУЛЕТКА ===
ROULETTE_NUMBERS = {
    0: "green",
    1: "red",
    2: "black",
    3: "red",
    4: "black",
    5: "red",
    6: "black",
    7: "red",
    8: "black",
    9: "red",
    10: "black",
    11: "black",
    12: "red",
    13: "black",
    14: "red",
    15: "black",
    16: "red",
    17: "black",
    18: "red",
    19: "red",
    20: "black",
    21: "red",
    22: "black",
    23: "red",
    24: "black",
    25: "red",
    26: "black",
    27: "red",
    28: "black",
    29: "black",
    30: "red",
    31: "black",
    32: "red",
    33: "black",
    34: "red",
    35: "black",
    36: "red",
}


@app.get("/games/roulette", response_class=HTMLResponse)
async def roulette_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="roulette.html", context={"user": user}
    )


@app.post("/api/roulette/spin")
async def roulette_spin(
    bet_amount: float = Form(...),
    bet_type: str = Form(...),
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401)
    if not is_cent_amount(bet_amount) or not 0.01 <= bet_amount <= MAX_BALANCE / 100:
        raise HTTPException(400, "Некорректная ставка")
    if bet_type not in ["red", "black", "green", "even", "odd"]:
        raise HTTPException(400, "Неверный тип ставки")
    if not user:
        raise HTTPException(401)
    if user.balance < bet_amount:
        raise HTTPException(400, "Недостаточно средств")
    user.balance -= bet_amount
    result_number = random.randint(0, 36)
    result_color = ROULETTE_NUMBERS[result_number]
    won = False
    mult = 0
    if bet_type == "green" and result_color == "green":
        won, mult = True, 36
    elif bet_type == "red" and result_color == "red":
        won, mult = True, 2
    elif bet_type == "black" and result_color == "black":
        won, mult = True, 2
    elif bet_type == "even" and result_number != 0 and result_number % 2 == 0:
        won, mult = True, 2
    elif bet_type == "odd" and result_number != 0 and result_number % 2 != 0:
        won, mult = True, 2
    win_amount = bet_amount * mult if won else 0
    if won:
        user.balance += win_amount
    log_game(
        db,
        user.id,
        "roulette",
        "won" if won else "lost",
        win_amount - bet_amount if won else -bet_amount,
        f"{result_number} {result_color} ({bet_type})",
    )
    db.flush()
    return {
        "ok": True,
        "number": result_number,
        "color": result_color,
        "won": won,
        "win_amount": win_amount,
        "balance": user.balance,
        "message": f"Выпало {result_number} ({result_color})"
        + (f" — Победа! +{win_amount:.2f}" if won else " — Проигрыш"),
    }


# === ЕЖЕДНЕВНЫЙ БОНУС ===
DAILY_BONUS_AMOUNT = 100.0
DAILY_BONUS_COOLDOWN = 86400


@app.post("/api/daily-bonus")
async def claim_daily_bonus(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    now = datetime.now()
    last_claim = user.last_daily_bonus
    if last_claim:
        diff = (now - last_claim).total_seconds()
        if diff < DAILY_BONUS_COOLDOWN:
            remaining = DAILY_BONUS_COOLDOWN - diff
            return {
                "ok": False,
                "reason": "cooldown",
                "remaining_seconds": int(remaining),
                "message": f"Доступен через {int(remaining // 3600)}ч {int((remaining % 3600) // 60)}мин",
            }
        user.daily_streak = (
            (user.daily_streak or 0) + 1 if diff < DAILY_BONUS_COOLDOWN * 2 else 1
        )
    else:
        user.daily_streak = 1
    streak_bonus = min((user.daily_streak // 7) * 10, 50)
    total_bonus = DAILY_BONUS_AMOUNT + streak_bonus
    user.balance += total_bonus
    user.last_daily_bonus = now
    log_game(
        db,
        user.id,
        "daily",
        "bonus",
        total_bonus,
        f"🎁 Стрик: {user.daily_streak}, бонус +{streak_bonus}",
    )
    db.flush()
    return {
        "ok": True,
        "amount": total_bonus,
        "streak": user.daily_streak,
        "streak_bonus": streak_bonus,
        "balance": user.balance,
    }


@app.get("/api/daily-bonus/status")
async def daily_bonus_status(user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    now = datetime.now()
    last_claim = user.last_daily_bonus
    streak = user.daily_streak or 0
    if last_claim:
        diff = (now - last_claim).total_seconds()
        available = diff >= DAILY_BONUS_COOLDOWN
        remaining = max(0, DAILY_BONUS_COOLDOWN - diff)
    else:
        available, remaining = True, 0
    next_streak = streak + 1 if last_claim and diff < DAILY_BONUS_COOLDOWN * 2 else 1
    streak_bonus = min((next_streak // 7) * 10, 50)
    return {
        "available": available,
        "remaining_seconds": int(remaining),
        "streak": streak,
        "next_bonus": DAILY_BONUS_AMOUNT + streak_bonus,
        "streak_bonus": streak_bonus,
    }


# === CRASH ===
def generate_crash_point():
    r = random.random()
    if r < 0.20:
        return 1.00
    crash = 0.89 / (1 - r)
    return round(max(1.00, min(crash, 25.0)), 2)


class CrashGame:
    def __init__(self, user_id, bet, target_multiplier=None):
        self.user_id = user_id
        self.bet = bet
        self.target_multiplier = target_multiplier
        self.crash_point = generate_crash_point()
        self.started_at = time.time()
        self.status = "running"
        self.cashout_multiplier = None
        self.win_amount = 0
        self.settlement_done = False

    def get_current_multiplier(self):
        if self.status != "running":
            return self.cashout_multiplier or self.crash_point
        elapsed = max(0.0, time.time() - self.started_at)
        if 0.08 * elapsed >= math.log(self.crash_point):
            return self.crash_point
        return math.exp(0.08 * elapsed)

    def check_crash(self):
        if self.status != "running":
            return False
        current = self.get_current_multiplier()
        # An auto target reached before the crash wins even if polling was delayed.
        if (
            self.target_multiplier
            and self.target_multiplier < self.crash_point
            and current >= self.target_multiplier
        ):
            self.cashout_at(self.target_multiplier)
            return False
        if current >= self.crash_point:
            self.status = "crashed"
            self.cashout_multiplier = self.crash_point
            return True
        return False

    def cashout_at(self, multiplier):
        if self.status != "running":
            return False
        self.status = "cashed_out"
        self.cashout_multiplier = multiplier
        self.win_amount = self.bet * multiplier
        return True


@app.get("/games/crash", response_class=HTMLResponse)
async def crash_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="crash.html", context={"user": user}
    )


@app.post("/api/crash/start")
async def crash_start(
    bet: float = Form(...),
    target: float = Form(0),
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401)
    if not is_cent_amount(bet) or not 0.01 <= bet <= MAX_BALANCE / 100:
        raise HTTPException(400, "Некорректная ставка")
    if not is_finite_number(target) or (target != 0 and not 1.01 <= target <= 25):
        raise HTTPException(400, "Некорректный авто-кешаут")
    old_game = crash_games.get(user.id)
    if old_game:
        old_game.check_crash()
        if old_game.status == "running":
            raise HTTPException(400, "Игра уже началась")
        await _settle_crash_game(old_game, user, db)
    if user.balance < bet:
        raise HTTPException(400, "Недостаточно средств")
    user.balance -= bet
    game = CrashGame(user.id, float(bet), target if target >= 1.01 else None)
    crash_games[user.id] = game
    db.flush()
    return {"ok": True, "balance": user.balance, "target": game.target_multiplier}


async def _settle_crash_game(game, user, db):
    """Однократно фиксирует результат Crash в балансе и истории."""
    if game.settlement_done:
        return
    if game.status == "crashed":
        log_game(
            db, user.id, "crash", "lost", -game.bet, f"Краш на {game.crash_point}x"
        )
    elif game.status == "cashed_out":
        gross = game.win_amount
        commission = gross * 0.05
        net_win = gross - commission
        net_win = money(
            Decimal(str(game.bet))
            * Decimal(str(game.cashout_multiplier))
            * Decimal("0.95")
        )
        user.balance += net_win
        game.win_amount = net_win
        profit = round(net_win - game.bet, 2)
        result = "won" if profit > 0 else "lost" if profit < 0 else "push"
        log_game(
            db,
            user.id,
            "crash",
            result,
            profit,
            f"Кешаут x{game.cashout_multiplier}, комиссия {commission:.2f}",
        )
    else:
        return
    game.settlement_done = True
    db.flush()


@app.post("/api/crash/cashout")
async def crash_cashout(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    game = crash_games.get(user.id)
    if not game:
        raise HTTPException(400, "Активной игры нет")
    game.check_crash()
    if game.status == "running":
        game.cashout_at(game.get_current_multiplier())
    await _settle_crash_game(game, user, db)
    return {
        "ok": True,
        "status": game.status,
        "multiplier": game.cashout_multiplier or game.crash_point,
        "win": game.win_amount,
        "balance": user.balance,
    }


@app.get("/api/crash/state")
async def crash_state(user=Depends(get_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(401)
    game = crash_games.get(user.id)
    if not game:
        return {"active": False, "balance": user.balance}
    if game.status == "running":
        game.check_crash()
    if game.status in ("crashed", "cashed_out"):
        await _settle_crash_game(game, user, db)
    return {
        "active": True,
        "status": game.status,
        "bet": game.bet,
        "target": game.target_multiplier,
        "crash_point": game.crash_point if game.status != "running" else None,
        "current_multiplier": round(game.get_current_multiplier(), 2),
        "win_amount": game.win_amount,
        "balance": user.balance,
    }


# === ПОКЕР ===
RANK_ORDER = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}


def evaluate_hand(cards):
    best = None
    for combo in itertools.combinations(cards, 5):
        score = _score_5(combo)
        if best is None or score[0] > best[0]:
            best = score
    return best


def _score_5(cards):
    ranks = sorted([RANK_ORDER[c["value"]] for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    is_flush = len(set(suits)) == 1
    is_straight = False
    straight_high = 0
    unique = sorted(set(ranks), reverse=True)
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            is_straight = True
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5
    counts = Counter(ranks)
    freq = sorted(counts.values(), reverse=True)
    if is_flush and is_straight:
        return (
            (9, straight_high),
            "Роял-флеш" if straight_high == 14 else "Стрит-флеш",
        )
    if freq == [4, 1]:
        return (
            (
                8,
                [r for r, c in counts.items() if c == 4][0],
                [r for r, c in counts.items() if c == 1][0],
            ),
            "Каре",
        )
    if freq == [3, 2]:
        return (
            (
                7,
                [r for r, c in counts.items() if c == 3][0],
                [r for r, c in counts.items() if c == 2][0],
            ),
            "Фулл-хаус",
        )
    if is_flush:
        return ((6,) + tuple(ranks), "Флеш")
    if is_straight:
        return ((5, straight_high), "Стрит")
    if freq == [3, 1, 1]:
        return (
            (4, [r for r, c in counts.items() if c == 3][0])
            + tuple(sorted([r for r, c in counts.items() if c == 1], reverse=True)),
            "Тройка",
        )
    if freq == [2, 2, 1]:
        return (
            (3,)
            + tuple(sorted([r for r, c in counts.items() if c == 2], reverse=True))
            + ([r for r, c in counts.items() if c == 1][0],),
            "Две пары",
        )
    if freq == [2, 1, 1, 1]:
        return (
            (2, [r for r, c in counts.items() if c == 2][0])
            + tuple(sorted([r for r, c in counts.items() if c == 1], reverse=True)),
            "Пара",
        )
    return ((1,) + tuple(ranks), "Старшая карта")


POKER_TURN_TIMEOUT = 30
POKER_SHOWDOWN_DURATION = 4


class PokerTable:
    def __init__(
        self,
        table_id,
        creator_id,
        creator_name,
        is_private=False,
        max_players=6,
        small_blind=10,
        big_blind=20,
    ):
        self.id = table_id
        self.creator_id = creator_id
        self.is_private = is_private
        self.max_players = max(2, min(int(max_players), 12))
        self.small_blind = float(small_blind)
        self.big_blind = float(big_blind)
        self.players = []
        self.deck = []
        self.community = []
        self.folded_cards = []
        self.phase = "waiting"
        self.current_turn = 0
        self.pot = 0.0
        self.current_bet = 0.0
        self.dealer_position = 0
        self.last_action_time = time.time()
        self.created_at = datetime.now()
        self.chat_messages = []
        self.hand_number = 0
        self.hand_payout_done = False
        self.hand_archived = False
        self.hand_players = []
        self.hand_actions = []
        self.hand_started_at = None
        self.action_revision = 0
        self.last_pot = 0.0
        self.preflop_bb_index = None
        self.preflop_raise_occurred = False
        self.showdown_until = 0.0
        self.min_raise = self.big_blind
        self.add_player(creator_id, creator_name, creator_name, 1000.0)

    def add_player(self, user_id, username, display_name=None, balance=1000):
        if self.phase != "waiting":
            return False
        if len(self.players) >= self.max_players or any(
            p["user_id"] == user_id for p in self.players
        ):
            return False
        self.players.append(
            {
                "user_id": user_id,
                "username": username,
                "display_name": display_name or username,
                "balance": max(0.0, float(balance)),
                "cards": [],
                "bet": 0.0,
                "invested": 0.0,
                "folded": False,
                "all_in": False,
                "connected": True,
                "leave_pending": False,
                "last_seen": time.time(),
                "synced_balance": float(balance),
                "acted": False,
                "raise_allowed": True,
                "participated": False,
                "returned": 0.0,
                "history_logged": False,
            }
        )
        return True

    def remove_player(self, user_id):
        idx = next(
            (i for i, p in enumerate(self.players) if p["user_id"] == user_id), None
        )
        if idx is None:
            return False
        player = self.players[idx]
        if self.phase not in ("waiting", "showdown"):
            was_current = idx == self.current_turn
            player["connected"] = False
            player["leave_pending"] = True
            # All-in chips remain eligible. A player who has already folded still
            # stays in the hand's accounting until every pot has been settled.
            if not player["folded"] and not player["all_in"]:
                self.record_action(player, "leave")
                player["folded"] = True
                player["cards"] = []
            self.add_chat(f"👋 {player['display_name']} покинул стол")
            if was_current or len(self._live_players()) <= 1:
                self.next_turn(idx if was_current else None)
            return True
        self.players.pop(idx)
        if self.players:
            self.dealer_position %= len(self.players)
            self.current_turn %= len(self.players)
        else:
            self.dealer_position = self.current_turn = 0
        return True

    def shuffle_deck(self):
        self.deck = create_deck()
        random.shuffle(self.deck)

    def deal_cards(self):
        self.shuffle_deck()
        self.folded_cards = []
        for player in self.players:
            if player["connected"] and not player["folded"]:
                player["cards"] = [self.deck.pop(), self.deck.pop()]

    def start_hand(self):
        if len(self.players) < 2:
            return False
        eligible = [p for p in self.players if p["connected"] and p["balance"] > 0]
        if len(eligible) < 2:
            return False
        self.hand_number += 1
        self.hand_archived = False
        self.hand_actions = []
        self.hand_started_at = datetime.now().isoformat()
        self.action_revision = getattr(self, "action_revision", 0) + 1
        self.community = []
        self.folded_cards = []
        self.pot = 0.0
        self.last_pot = 0.0
        self.current_bet = 0.0
        self.hand_payout_done = False
        self.preflop_raise_occurred = False
        self.min_raise = self.big_blind
        self.phase = "preflop"
        for player in self.players:
            player["cards"] = []
            player["bet"] = 0.0
            player["invested"] = 0.0
            player["folded"] = (not player["connected"]) or player["balance"] <= 0
            player["all_in"] = False
            player["acted"] = False
            player["raise_allowed"] = True
            player["participated"] = not player["folded"]
            player["returned"] = 0.0
            player["history_logged"] = False
            player["last_action"] = None
        self.deal_cards()
        self.hand_players = json.loads(
            json.dumps(
                [
                    {"user_id": p["user_id"], "cards": p["cards"]}
                    for p in self.players
                    if p["participated"]
                ]
            )
        )
        active = [i for i, p in enumerate(self.players) if not p["folded"]]
        if len(active) < 2:
            self.phase = "waiting"
            return False
        dealer_idx = next(
            (self.dealer_position + step) % len(self.players)
            for step in range(1, len(self.players) + 1)
            if (self.dealer_position + step) % len(self.players) in active
        )
        self.dealer_position = dealer_idx
        dealer_offset = active.index(dealer_idx)
        if len(active) == 2:
            sb_idx = dealer_idx
            bb_idx = active[(dealer_offset + 1) % len(active)]
            first_idx = dealer_idx
        else:
            sb_idx = active[(dealer_offset + 1) % len(active)]
            bb_idx = active[(dealer_offset + 2) % len(active)]
            first_idx = active[(dealer_offset + 3) % len(active)]
        sb = self.players[sb_idx]
        bb = self.players[bb_idx]
        sb_amount = min(self.small_blind, sb["balance"])
        bb_amount = min(self.big_blind, bb["balance"])
        self._commit_bet(sb, sb_amount)
        self._commit_bet(bb, bb_amount)
        self.record_action(sb, "small_blind", sb_amount, sb["bet"])
        self.record_action(bb, "big_blind", bb_amount, bb["bet"])
        self.pot = sb_amount + bb_amount
        sb["all_in"] = sb["balance"] <= 1e-9
        bb["all_in"] = bb["balance"] <= 1e-9
        self.current_bet = self.big_blind
        self.current_turn = first_idx
        self.preflop_bb_index = bb_idx
        self.last_action_time = time.time()
        self.add_chat(
            f"🎴 Раздача #{self.hand_number} началась | Дилер: {self.players[dealer_idx]['display_name']}"
        )
        if (
            self.players[self.current_turn]["all_in"]
            or len(self.get_active_players()) <= 1
        ):
            self.next_turn()
        return True

    @staticmethod
    def _commit_bet(player, amount):
        amount = round(max(0.0, min(float(amount), player["balance"])), 2)
        player["balance"] = round(player["balance"] - amount, 2)
        player["bet"] = round(player["bet"] + amount, 2)
        player["invested"] = round(player["invested"] + amount, 2)

    def get_active_players(self):
        return [
            p
            for p in self.players
            if p["connected"] and not p["folded"] and not p["all_in"]
        ]

    def _live_players(self):
        return [p for p in self.players if not p["folded"]]

    def action_options(self, user_id):
        player = next((p for p in self.players if p["user_id"] == user_id), None)
        is_turn = bool(
            player
            and self.phase in {"preflop", "flop", "turn", "river"}
            and 0 <= self.current_turn < len(self.players)
            and self.players[self.current_turn]["user_id"] == user_id
            and player["connected"]
            and not player["folded"]
            and not player["all_in"]
        )
        to_call = money(max(0.0, self.current_bet - player["bet"])) if player else 0.0
        min_target = money(
            self.current_bet + self.min_raise
            if self.current_bet > 0
            else self.big_blind
        )
        max_target = money(player["bet"] + player["balance"]) if player else 0.0
        return {
            "is_your_turn": is_turn,
            "to_call": to_call,
            "can_check": is_turn and to_call == 0,
            "can_call": is_turn and to_call > 0,
            "call_amount": money(min(to_call, player["balance"])) if player else 0.0,
            "can_raise": is_turn
            and player.get("raise_allowed", True)
            and max_target >= min_target,
            "can_all_in": is_turn
            and player["balance"] > 0
            and (player.get("raise_allowed", True) or max_target <= self.current_bet),
            "min_raise_to": min_target,
            "max_raise_to": max_target,
            "current_player_bet": player["bet"] if player else 0.0,
            "all_in_amount": player["balance"] if player else 0.0,
        }

    def record_action(self, player, kind, amount=0, target=None):
        item = {
            "user_id": player["user_id"],
            "kind": kind,
            "amount": money(amount),
            "target": money(target) if target is not None else None,
            "phase": self.phase,
            "timestamp": datetime.now().isoformat(),
        }
        self.hand_actions = getattr(self, "hand_actions", []) + [item]
        player["last_action"] = {
            k: item[k] for k in ("kind", "amount", "target", "phase")
        }
        self.action_revision = getattr(self, "action_revision", 0) + 1

    def next_turn(self, actor_idx=None):
        if actor_idx is not None:
            self.players[actor_idx]["acted"] = True
            self.players[actor_idx]["raise_allowed"] = False
        live = self._live_players()
        if len(live) <= 1:
            self.end_hand()
            return
        active = self.get_active_players()
        matched_last_opponent = (
            len(active) == 1
            and active[0]["bet"]
            >= max((p["bet"] for p in live if p is not active[0]), default=0) - 1e-9
        )
        if not active or matched_last_opponent:
            self.showdown()
            return
        needs_action = [
            p for p in active if not p["acted"] or p["bet"] < self.current_bet - 1e-9
        ]
        if not needs_action:
            self.advance_phase()
            return
        for step in range(1, len(self.players) + 1):
            idx = (self.current_turn + step) % len(self.players)
            if self.players[idx] in needs_action:
                self.current_turn = idx
                break
        self.last_action_time = time.time()

    def reopen_action(self, actor_idx, increment):
        if increment >= self.min_raise - 1e-9:
            self.min_raise = increment
            for idx, player in enumerate(self.players):
                if idx != actor_idx:
                    player["acted"] = False
                    player["raise_allowed"] = True

    def advance_phase(self):
        if self.phase == "preflop":
            self.phase = "flop"
            self.community = [self.deck.pop() for _ in range(3)]
        elif self.phase == "flop":
            self.phase = "turn"
            self.community.append(self.deck.pop())
        elif self.phase == "turn":
            self.phase = "river"
            self.community.append(self.deck.pop())
        elif self.phase == "river":
            self.showdown()
            return
        for player in self.players:
            player["bet"] = 0.0
            player["acted"] = False
            player["raise_allowed"] = True
        self.current_bet = 0.0
        self.min_raise = self.big_blind
        self.preflop_raise_occurred = False
        active = self.get_active_players()
        if len(active) <= 1:
            self.showdown()
            return
        if active:
            start = self.dealer_position % len(self.players)
            for step in range(1, len(self.players) + 1):
                idx = (start + step) % len(self.players)
                if not self.players[idx]["folded"] and not self.players[idx]["all_in"]:
                    self.current_turn = idx
                    break
        else:
            self.showdown()
            return
        self.last_action_time = time.time()

    def showdown(self):
        if self.hand_payout_done:
            return
        self.phase = "showdown"
        active_players = self._live_players()
        self.last_pot = self.pot
        self.payouts = []

        if len(active_players) > 1 and len(self.community) < 5:
            while len(self.community) < 5 and self.deck:
                self.community.append(self.deck.pop())
        scores = {}
        for p in active_players:
            if len(p["cards"]) == 2 and len(self.community) >= 5:
                score = evaluate_hand(p["cards"] + self.community)
                if score is not None:
                    scores[id(p)] = score

        # Строим main/side pots по уровням вложений. Это корректно учитывает
        # короткий all-in, который раньше выпадал из главного банка.
        levels = sorted({p["invested"] for p in self.players if p["invested"] > 1e-9})
        previous = 0.0
        for level in levels:
            contributors = [p for p in self.players if p["invested"] >= level - 1e-9]
            pot_part = (level - previous) * len(contributors)
            if pot_part <= 1e-9:
                previous = level
                continue
            if len(contributors) == 1:
                contributor = contributors[0]
                contributor["balance"] += pot_part
                contributor["returned"] += pot_part
                previous = level
                continue
            eligible = [
                p
                for p in active_players
                if p["invested"] >= level - 1e-9 and id(p) in scores
            ]
            if eligible:
                best_score = max(scores[id(p)][0] for p in eligible)
                winners = [p for p in eligible if scores[id(p)][0] == best_score]
                # Award indivisible cents clockwise after the dealer.
                winners.sort(
                    key=lambda p: (
                        (self.players.index(p) - self.dealer_position - 1)
                        % len(self.players)
                    )
                )
                cents, extra = divmod(round(pot_part * 100), len(winners))
                for index, winner in enumerate(winners):
                    share = (cents + (index < extra)) / 100
                    winner["balance"] = round(winner["balance"] + share, 2)
                    winner["returned"] = round(winner["returned"] + share, 2)
                    self.payouts.append(
                        {
                            "user_id": winner["user_id"],
                            "amount": share,
                            "hand": scores[id(winner)][1],
                        }
                    )
            elif len(active_players) == 1:
                winner = active_players[0]
                winner["balance"] += pot_part
                winner["returned"] += pot_part
                self.payouts.append(
                    {
                        "user_id": winner["user_id"],
                        "amount": pot_part,
                        "hand": "Без вскрытия",
                    }
                )
            else:
                # Uncalled overbets and a pot with no eligible player are refunds.
                share = pot_part / len(contributors)
                for contributor in contributors:
                    contributor["balance"] += share
                    contributor["returned"] += share
            previous = level

        self.hand_payout_done = True
        if len(active_players) == 1:
            self.add_chat(
                f"🏆 {active_players[0]['display_name']} выигрывает {self.pot:.2f}"
            )
        elif scores:
            best = max(scores.values(), key=lambda x: x[0])
            winners = [
                p
                for p in active_players
                if id(p) in scores and scores[id(p)][0] == best[0]
            ]
            names = ", ".join(p["display_name"] for p in winners)
            best_hand = best[1] if len(best) > 1 else "Комбинация"
            self.add_chat(f"🏆 {names} выиграли банк {self.pot:.2f} ({best_hand})")
        self.showdown_until = time.time() + POKER_SHOWDOWN_DURATION
        self.last_action_time = time.time()

    def reset_after_showdown(self):
        if (
            self.phase != "showdown"
            or not self.hand_payout_done
            or time.time() < self.showdown_until
        ):
            return False
        self.phase = "waiting"
        self.community = []
        self.deck = []
        self.folded_cards = []
        self.pot = 0.0
        self.current_bet = 0.0
        self.min_raise = self.big_blind
        self.current_turn = 0
        self.preflop_bb_index = None
        self.preflop_raise_occurred = False
        self.showdown_until = 0.0
        self.players = [p for p in self.players if not p.get("leave_pending")]
        for player in self.players:
            player["cards"] = []
            player["bet"] = 0.0
            player["invested"] = 0.0
            player["folded"] = False
            player["all_in"] = False
            player["connected"] = True
        if self.players:
            self.dealer_position %= len(self.players)
            self.current_turn = 0
            if not any(p["user_id"] == self.creator_id for p in self.players):
                self.creator_id = self.players[0]["user_id"]
        else:
            self.dealer_position = self.current_turn = 0
        return True

    def end_hand(self):
        self.showdown()
        self.last_action_time = time.time()

    def add_chat(self, message):
        self.chat_messages.append(
            {"message": message, "timestamp": datetime.now().strftime("%H:%M:%S")}
        )
        if len(self.chat_messages) > 50:
            self.chat_messages = self.chat_messages[-50:]


@app.get("/games/poker", response_class=HTMLResponse)
async def poker_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return RedirectResponse("/games/poker/lobby")


@app.get("/games/poker/lobby", response_class=HTMLResponse)
async def poker_lobby_page(request: Request, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="poker_lobby.html", context={"user": user}
    )


@app.get("/games/poker/table/{table_id}", response_class=HTMLResponse)
async def poker_table_page(request: Request, table_id: str, user=Depends(get_user)):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request,
        name="poker_multiplayer.html",
        context={"user": user, "table_id": table_id},
    )


@app.get("/games/poker/history", response_class=HTMLResponse)
async def poker_history_page(
    request: Request,
    before: int | None = None,
    player_id: int | None = None,
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login")
    if player_id is not None and player_id != user.id and not user.is_admin:
        raise HTTPException(403, "Доступна только ваша история")
    query = db.query(PokerHand)
    target = player_id if user.is_admin else user.id
    if target is not None:
        query = query.join(
            PokerHandPlayer, PokerHand.id == PokerHandPlayer.hand_id
        ).filter(PokerHandPlayer.user_id == target)
    if before is not None:
        query = query.filter(PokerHand.id < before)
    rows = query.order_by(PokerHand.id.desc()).limit(41).all()
    next_url = None
    if len(rows) > 40:
        params = {"before": rows[39].id}
        if player_id is not None:
            params["player_id"] = player_id
        next_url = "/games/poker/history?" + urlencode(params)
    return templates.TemplateResponse(
        request=request,
        name="poker_history.html",
        context={
            "user": user,
            "hands": [visible_hand(r, user) for r in rows[:40]],
            "next_url": next_url,
            "player_id": player_id,
        },
    )


@app.get("/games/poker/history/{hand_id}", response_class=HTMLResponse)
async def poker_hand_page(
    request: Request,
    hand_id: int,
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login")
    row = db.get(PokerHand, hand_id)
    if not row:
        raise HTTPException(404, "Раздача не найдена")
    if not user.is_admin and not db.get(PokerHandPlayer, (hand_id, user.id)):
        raise HTTPException(403, "Эта раздача доступна только участникам")
    return templates.TemplateResponse(
        request=request,
        name="poker_history.html",
        context={
            "user": user,
            "hands": [visible_hand(row, user)],
            "next_url": None,
            "player_id": None,
            "detail": True,
        },
    )


@app.get("/api/poker/tables")
async def list_tables(user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    tables = []
    for table_id, table in poker_tables.items():
        tables.append(
            {
                "id": table_id,
                **table_management("poker", table, is_super_admin(user)),
                "creator": next(
                    (
                        p["display_name"]
                        for p in table.players
                        if p["user_id"] == table.creator_id
                    ),
                    "Unknown",
                ),
                "players": len(table.players),
                "max_players": table.max_players,
                "is_private": table.is_private,
                "phase": table.phase,
                "small_blind": table.small_blind,
                "big_blind": table.big_blind,
                "created_at": table.created_at.strftime("%H:%M"),
            }
        )
    return {"tables": tables}


@app.post("/api/poker/create-table")
async def create_table(
    is_private: bool = Form(False),
    max_players: int = Form(6),
    small_blind: float = Form(10),
    big_blind: float = Form(20),
    user=Depends(get_user),
):
    if not user:
        raise HTTPException(401)
    if max_players < 2 or max_players > 12:
        raise HTTPException(400, "Игроков: от 2 до 12")
    if (
        not is_cent_amount(small_blind)
        or not is_cent_amount(big_blind)
        or not 0.01 <= small_blind < big_blind <= MAX_BALANCE
    ):
        raise HTTPException(400, "Блайнды должны быть > 0")
    if small_blind >= big_blind:
        raise HTTPException(400, "Малый блайнд < большого")
    ensure_one_table(user.id)
    table_id = "table_" + secrets.token_hex(12)
    table = PokerTable(
        table_id,
        user.id,
        user.display_name or user.username,
        is_private,
        max_players,
        small_blind,
        big_blind,
    )
    table.players[0]["balance"] = table.players[0]["synced_balance"] = float(
        user.balance
    )
    poker_tables[table_id] = table
    return {"ok": True, "table_id": table_id}


@app.post("/api/poker/join-table/{table_id}")
async def join_table(table_id: str, user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        raise HTTPException(404)
    if any(p["user_id"] == user.id and p.get("leave_pending") for p in table.players):
        raise HTTPException(409, "Дождитесь завершения предыдущей раздачи")
    if any(p["user_id"] == user.id for p in table.players):
        return {"ok": True, "message": "Уже в игре"}
    ensure_one_table(user.id, table_id)
    if table.phase != "waiting":
        raise HTTPException(409, "Дождитесь окончания раздачи")
    if table.is_private:
        if table_id not in table_requests:
            table_requests[table_id] = []
        if any(r["user_id"] == user.id for r in table_requests[table_id]):
            return {"ok": False, "message": "Заявка уже отправлена"}
        table_requests[table_id].append(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name or user.username,
                "timestamp": time.time(),
            }
        )
        return {"ok": True, "message": "Заявка отправлена", "pending": True}
    else:
        table = poker_tables.get(table_id)
        if not table:
            raise HTTPException(404)
        if not table.add_player(
            user.id, user.username, user.display_name, user.balance
        ):
            raise HTTPException(400, "Стол заполнен")
        return {"ok": True, "message": "Присоединились"}


@app.get("/api/poker/table/{table_id}")
async def get_table_state(table_id: str, user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        return {"active": False}
    if table.is_private and not any(p["user_id"] == user.id for p in table.players):
        raise HTTPException(403, "Доступ к приватному столу только для участников")
    refresh_table_balances(current_db.get(), table)
    table.reset_after_showdown()
    is_creator = user.id == table.creator_id
    state = {
        "active": True,
        "table_id": table_id,
        "phase": table.phase,
        "players": [],
        "community": table.community,
        "deck_count": len(table.deck),
        "folded_cards": [],
        "pot": table.pot,
        "payouts": getattr(table, "payouts", []),
        "current_bet": table.current_bet,
        "current_turn": table.current_turn,
        "max_players": table.max_players,
        "dealer_position": table.dealer_position,
        "hand_number": table.hand_number,
        "action_revision": getattr(table, "action_revision", 0),
        "turn_remaining": max(
            0, POKER_TURN_TIMEOUT - (time.time() - table.last_action_time)
        )
        if table.phase in {"preflop", "flop", "turn", "river"}
        else 0,
        "turn_timeout": POKER_TURN_TIMEOUT,
        "history_hand_id": (
            current_db.get()
            .query(PokerHand.id)
            .filter_by(table_id=table.id, hand_number=table.hand_number)
            .scalar()
            if table.hand_payout_done
            else None
        ),
        "your_actions": table.action_options(user.id),
        "is_creator": is_creator,
        "small_blind": table.small_blind,
        "big_blind": table.big_blind,
        "min_raise": table.min_raise,
        "last_pot": table.last_pot,
        "chat": table.chat_messages[-20:],
        "requests": [],
    }
    if is_creator and table_id in table_requests:
        state["requests"] = table_requests[table_id]
    for i, player in enumerate(table.players):
        player_data = {
            "user_id": player["user_id"],
            "display_name": player["display_name"],
            "balance": player["balance"],
            "bet": player["bet"],
            "invested": player["invested"],
            "folded": player["folded"],
            "all_in": player["all_in"],
            "connected": player["connected"],
            "last_action": player.get("last_action"),
            "is_current_turn": i == table.current_turn
            and table.phase in {"preflop", "flop", "turn", "river"},
        }
        show_cards = player["user_id"] == user.id or (
            table.phase == "showdown" and not player["folded"]
        )
        if show_cards:
            player_data["cards"] = player["cards"]
        else:
            player_data["cards"] = [{"value": "?", "suit": "?"}] * len(player["cards"])
        state["players"].append(player_data)
    return state


@app.post("/api/poker/table/{table_id}/action")
async def table_action(
    table_id: str,
    action: str = Form(...),
    raise_amount: float = Form(0),
    expected_hand: int | None = Form(None),
    expected_revision: int | None = Form(None),
    expected_all_in_amount: float | None = Form(None),
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        raise HTTPException(404)
    if (expected_hand is not None and expected_hand != table.hand_number) or (
        expected_revision is not None
        and expected_revision != getattr(table, "action_revision", 0)
    ):
        raise HTTPException(
            409, "Ход уже изменился. Стол обновлён — проверьте доступные действия."
        )
    refresh_table_balances(db, table)
    player = next((p for p in table.players if p["user_id"] == user.id), None)
    if not player:
        raise HTTPException(400, "Вы не в игре")
    if table.phase in ("waiting", "showdown"):
        raise HTTPException(400, "Сейчас нельзя")
    if table.current_turn < 0 or table.current_turn >= len(table.players):
        raise HTTPException(409, "Состояние стола устарело")
    current_player = table.players[table.current_turn]
    if current_player["user_id"] != user.id:
        raise HTTPException(400, "Не ваш ход")
    if not player["connected"] or player["folded"] or player["all_in"]:
        raise HTTPException(400, "Вы уже не можете")
    actor_idx = table.current_turn
    if action == "fold":
        table.record_action(player, "fold")
        player["folded"] = True
        table.folded_cards.extend(player["cards"])
        player["cards"] = []
        table.add_chat(f"{player['display_name']} сбросил карты")
        table.next_turn(actor_idx)
    elif action == "check":
        if table.action_options(user.id)["to_call"] > 0:
            raise HTTPException(400, "Нужно уравнять")
        table.add_chat(f"{player['display_name']} сделал чек")
        table.record_action(player, "check")
        table.next_turn(actor_idx)
    elif action == "call":
        to_call = max(0.0, table.current_bet - player["bet"])
        if to_call <= 1e-9:
            raise HTTPException(400, "Доступен чек")
        amount = min(to_call, player["balance"])
        table._commit_bet(player, amount)
        table.record_action(player, "call", amount, player["bet"])
        table.pot += amount
        if player["balance"] <= 1e-9:
            player["all_in"] = True
        table.add_chat(f"{player['display_name']} уравнял {amount:.2f}")
        table.next_turn(actor_idx)
    elif action == "raise":
        if not player["raise_allowed"]:
            raise HTTPException(400, "Короткий all-in не открывает повторный рейз")
        try:
            target = float(raise_amount)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, "Неверная сумма")
        if not is_cent_amount(target):
            raise HTTPException(400, "Неверная сумма")
        min_target = (
            table.current_bet + table.min_raise
            if table.current_bet > 0
            else table.big_blind
        )
        if target < min_target - 1e-9:
            raise HTTPException(400, f"Мин. рейз: {min_target:.2f}")
        if target <= player["bet"]:
            raise HTTPException(400, "Сумма должна быть выше")
        to_add = target - player["bet"]
        if to_add > player["balance"] + 1e-9:
            raise HTTPException(400, "Недостаточно фишек")
        previous_bet = table.current_bet
        table._commit_bet(player, to_add)
        table.record_action(player, "raise", to_add, player["bet"])
        table.pot += to_add
        table.current_bet = player["bet"]
        table.reopen_action(actor_idx, table.current_bet - previous_bet)
        if table.phase == "preflop":
            table.preflop_raise_occurred = True
        if player["balance"] <= 1e-9:
            player["all_in"] = True
        table.add_chat(f"{player['display_name']} повысил до {player['bet']:.2f}")
        table.next_turn(actor_idx)
    elif action == "all_in":
        amount = player["balance"]
        if expected_all_in_amount is not None and (
            not is_cent_amount(expected_all_in_amount)
            or abs(expected_all_in_amount - amount) > 1e-9
        ):
            raise HTTPException(
                409, "Баланс изменился. Подтвердите All-in с актуальной суммой."
            )
        previous_bet = table.current_bet
        if not player["raise_allowed"] and player["bet"] + amount > previous_bet + 1e-9:
            raise HTTPException(400, "Короткий all-in не открывает повторный рейз")
        if amount <= 1e-9:
            raise HTTPException(400, "Нет фишек")
        table._commit_bet(player, amount)
        table.record_action(player, "all_in", amount, player["bet"])
        table.pot += amount
        player["all_in"] = True
        if player["bet"] > table.current_bet:
            table.current_bet = player["bet"]
            table.reopen_action(actor_idx, table.current_bet - previous_bet)
            if table.phase == "preflop":
                table.preflop_raise_occurred = True
        table.add_chat(f"{player['display_name']} ALL-IN ({amount:.2f})")
        table.next_turn(actor_idx)
    else:
        raise HTTPException(400, "Неизвестное действие")
    sync_table_balances(db, table)
    return {"ok": True, "phase": table.phase, "current_turn": table.current_turn}


@app.post("/api/poker/table/{table_id}/start")
async def start_table_game(table_id: str, user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        raise HTTPException(404)
    if user.id != table.creator_id:
        raise HTTPException(403, "Только создатель")
    refresh_table_balances(current_db.get(), table)
    table.reset_after_showdown()
    if len([p for p in table.players if p["connected"] and p["balance"] > 0]) < 2:
        raise HTTPException(400, "Нужно минимум 2 игрока с фишками")
    if table.phase != "waiting":
        raise HTTPException(400, "Игра уже началась")
    if not table.start_hand():
        raise HTTPException(400, "Не удалось начать")
    table.add_chat("🎮 Новая раздача началась!")
    sync_table_balances(current_db.get(), table)
    return {"ok": True}


@app.post("/api/poker/table/{table_id}/approve/{request_user_id}")
async def approve_request(
    table_id: str,
    request_user_id: int,
    user=Depends(get_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table or user.id != table.creator_id:
        raise HTTPException(403)
    if table_id not in table_requests:
        raise HTTPException(404)
    request = next(
        (r for r in table_requests[table_id] if r["user_id"] == request_user_id), None
    )
    if not request:
        raise HTTPException(404)
    req_user = (
        db.query(User)
        .filter(User.deleted_at.is_(None))
        .filter(User.id == request_user_id)
        .first()
    )
    if not req_user:
        raise HTTPException(404)
    table = poker_tables.get(table_id)
    if not table:
        raise HTTPException(404)
    ensure_one_table(request_user_id, table_id)
    if not table.add_player(
        request_user_id, req_user.username, req_user.display_name, req_user.balance
    ):
        raise HTTPException(400, "Стол заполнен")
    table_requests[table_id] = [
        r for r in table_requests[table_id] if r["user_id"] != request_user_id
    ]
    table.add_chat(f"✅ {req_user.display_name or req_user.username} присоединился")
    return {"ok": True}


@app.post("/api/poker/table/{table_id}/decline/{request_user_id}")
async def decline_request(table_id: str, request_user_id: int, user=Depends(get_user)):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table or user.id != table.creator_id:
        raise HTTPException(403)
    if table_id in table_requests:
        table_requests[table_id] = [
            r for r in table_requests[table_id] if r["user_id"] != request_user_id
        ]
    return {"ok": True}


@app.post("/api/poker/table/{table_id}/chat")
async def send_chat_message(
    table_id: str, message: str = Form(...), user=Depends(get_user)
):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        raise HTTPException(404)
    player = next((p for p in table.players if p["user_id"] == user.id), None)
    if not player or not player["connected"]:
        raise HTTPException(400)
    clean = message.strip()[:200]
    if clean:
        table.add_chat(f"💬 {player['display_name']}: {clean}")
    return {"ok": True}


@app.post("/api/poker/table/{table_id}/leave")
async def leave_table(
    table_id: str, user=Depends(get_user), db: Session = Depends(get_db)
):
    if not user:
        raise HTTPException(401)
    table = poker_tables.get(table_id)
    if not table:
        return {"ok": True}
    refresh_table_balances(db, table)
    player = next((p for p in table.players if p["user_id"] == user.id), None)
    if not player:
        return {"ok": True}
    table.remove_player(user.id)
    if table.players:
        sync_table_balances(db, table)
        if table.creator_id == user.id:
            successors = [
                p for p in table.players if p["connected"] and not p["leave_pending"]
            ]
            if successors:
                table.creator_id = successors[0]["user_id"]
                table.add_chat(f"👑 {successors[0]['display_name']} теперь создатель")
    else:
        poker_tables.pop(table_id, None)
        table_requests.pop(table_id, None)
    return {"ok": True}


async def poker_timeout_checker():
    while True:
        await asyncio.sleep(1)
        try:
            async with transaction(serialize_runtime, restore_runtime) as db:
                for table_id, table in list(poker_tables.items()):
                    refresh_table_balances(db, table)
                    if not table.players:
                        poker_tables.pop(table_id, None)
                        table_requests.pop(table_id, None)
                        continue
                    if table.reset_after_showdown():
                        connected = [p for p in table.players if p["connected"]]
                        if not connected:
                            poker_tables.pop(table_id, None)
                            table_requests.pop(table_id, None)
                            continue
                        if not any(p["user_id"] == table.creator_id for p in connected):
                            table.creator_id = connected[0]["user_id"]
                    if table.phase in ("waiting", "showdown"):
                        continue
                    idx = table.current_turn
                    player = table.players[idx]
                    if player["folded"] or player["all_in"]:
                        table.next_turn()
                    elif time.time() - table.last_action_time >= POKER_TURN_TIMEOUT:
                        table.record_action(player, "timeout")
                        player["folded"] = True
                        player["cards"] = []
                        table.add_chat(f"⏱️ {player['display_name']} автофолд")
                        table.next_turn(idx)
                    sync_table_balances(db, table)
        except Exception:
            logger.exception("Poker timeout checker failed")


def serialize_runtime():
    data = {
        "version": 1,
        "last_tick": time.time(),
        "blackjack": {str(uid): game for uid, game in active_games.items()},
        "crash": {str(uid): game.__dict__ for uid, game in crash_games.items()},
        "poker": {
            tid: {**table.__dict__, "created_at": table.created_at.isoformat()}
            for tid, table in poker_tables.items()
        },
        "requests": table_requests,
        "durak": {tid: table.to_dict() for tid, table in durak_service.tables.items()},
        "billiards": {
            tid: table.to_dict() for tid, table in billiards_service.tables.items()
        },
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


def restore_runtime(payload):
    data = json.loads(payload)
    if data and data.get("version") != 1:
        raise RuntimeError("Unsupported saved game state version")
    active_games.clear()
    active_games.update(
        {int(uid): game for uid, game in data.get("blackjack", {}).items()}
    )
    crash_games.clear()
    for uid, attributes in data.get("crash", {}).items():
        game = CrashGame.__new__(CrashGame)
        game.__dict__.update(attributes)
        crash_games[int(uid)] = game
    poker_tables.clear()
    for tid, attributes in data.get("poker", {}).items():
        table = PokerTable.__new__(PokerTable)
        table.__dict__.update(attributes)
        for key, value in {
            "hand_archived": False,
            "hand_players": [],
            "hand_actions": [],
            "hand_started_at": None,
            "action_revision": 0,
        }.items():
            table.__dict__.setdefault(key, value)
        table.created_at = datetime.fromisoformat(attributes["created_at"])
        poker_tables[tid] = table
    gap = max(0.0, time.time() - data.get("last_tick", time.time()))
    if gap > 5:
        for game in crash_games.values():
            if game.status == "running":
                game.started_at += gap
        for game in active_games.values():
            if game.get("status") == "showdown":
                game["resolved_at"] += gap
        for table in poker_tables.values():
            if table.phase != "waiting":
                table.last_action_time += gap
                if table.phase == "showdown":
                    table.showdown_until += gap
    table_requests.clear()
    table_requests.update(data.get("requests", {}))
    durak_service.restore(data.get("durak", {}), gap if gap > 5 else 0)
    billiards_service.restore(data.get("billiards", {}), gap if gap > 5 else 0)


async def game_settlement_checker():
    while True:
        await asyncio.sleep(1)
        try:
            async with transaction(serialize_runtime, restore_runtime) as db:
                for uid, game in list(crash_games.items()):
                    user = db.query(User).filter(User.id == uid).first()
                    if not user:
                        crash_games.pop(uid, None)
                        continue
                    game.check_crash()
                    await _settle_crash_game(game, user, db)
                for uid, game in list(active_games.items()):
                    if (
                        game["status"] == "showdown"
                        and time.time() - game["resolved_at"] >= 4
                    ):
                        user = db.query(User).filter(User.id == uid).first()
                        if user:
                            _finalize_blackjack(user, game, db)
        except Exception:
            logger.exception("Failed to settle games")


async def durak_timeout_checker():
    while True:
        await asyncio.sleep(1)
        # Isolate settlement failures so one table cannot stop the other games.
        try:
            async with transaction(serialize_runtime, restore_runtime):
                ids = list(durak_service.tables)
            for tid in ids:
                try:
                    async with transaction(serialize_runtime, restore_runtime) as db:
                        durak_service.tick_table(tid, db)
                except Exception:
                    logger.exception("Failed to settle Durak table %s", tid)
        except Exception:
            logger.exception("Failed to read Durak tables")


async def billiards_timeout_checker():
    while True:
        await asyncio.sleep(1)
        try:
            async with transaction(serialize_runtime, restore_runtime):
                ids = list(billiards_service.tables)
            for tid in ids:
                try:
                    async with transaction(serialize_runtime, restore_runtime) as db:
                        billiards_service.tick_table(tid, db)
                except Exception:
                    logger.exception("Failed to settle billiards table %s", tid)
        except Exception:
            logger.exception("Failed to read billiards tables")


app.include_router(
    build_durak_router(durak_service, get_user, get_db, templates, is_super_admin)
)
app.include_router(
    build_billiards_router(
        billiards_service, get_user, get_db, templates, is_super_admin
    )
)

from backup_routes import make_backup_router

app.include_router(make_backup_router(is_super_admin, is_owner_admin))

# Registered last so rejected floods/body uploads never acquire the game lock.
from request_guard import RequestGuard, RequestGuardMiddleware

request_guard = RequestGuard()
app.add_middleware(
    RequestGuardMiddleware, guard=request_guard, routes=lambda: app.openapi()["paths"]
)

# ST9_SQUAD_BEGIN
from squad_routes import make_router as make_squad_router

app.include_router(
    make_squad_router(
        get_user,
        get_db,
        templates,
        is_senior_or_super,
        read_json_object,
        log_game,
        MAX_BALANCE,
        can_edit_user,
    )
)
# ST9_SQUAD_END

# ST9_SQUAD_SCHEDULER_BEGIN
from squad_scheduler import schedule_checker


async def squad_schedule_checker():
    await schedule_checker(
        transaction,
        serialize_runtime,
        restore_runtime,
        is_senior_or_super,
        log_game,
        MAX_BALANCE,
        can_edit_user,
    )


# ST9_SQUAD_SCHEDULER_END

# ST9_TABLE_ADMIN_BEGIN
from table_admin import make_router as make_table_admin_router

app.include_router(
    make_table_admin_router(
        get_user,
        get_db,
        is_super_admin,
        read_json_object,
        poker_tables,
        table_requests,
        durak_service,
        billiards_service,
        log_game,
    )
)
# ST9_TABLE_ADMIN_END

from home_activity import make_router as make_home_activity_router

app.include_router(
    make_home_activity_router(
        get_db,
        active_games,
        crash_games,
        poker_tables,
        durak_service,
        billiards_service,
    )
)
from admin_presence import make_router as make_presence_router

app.include_router(make_presence_router())
app.include_router(make_medals_router(get_user, get_db, templates))
from statistics_freshness import (
    make_router as make_freshness_router,
    may_view_statistics,
)

templates.env.globals["may_view_statistics"] = may_view_statistics

app.include_router(make_freshness_router(get_user, get_db))
app.include_router(make_automation_router(get_user, get_db, is_super_admin))
app.include_router(make_admin_hub_router(get_user, get_db, is_super_admin))


app.include_router(
    make_rule_tests_router(
        get_user, get_db, is_senior_or_super, templates, read_json_object
    )
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=COOKIE_HTTPS_ONLY,
    same_site="lax",
)

from http_security import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)

from distinction_reviews import make_router as make_distinction_review_router

app.include_router(make_distinction_review_router(get_user, get_db))
