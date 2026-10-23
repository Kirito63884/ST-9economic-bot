"""Discord notifications never directly mutate wallets or decide membership."""

from datetime import datetime
import json


def gateway_packet(raw):
    """discord.py 2.x emits complete JSON via on_socket_raw_receive."""
    if not isinstance(raw, (str, bytes)):
        return None
    try:
        packet = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return packet if isinstance(packet, dict) and packet.get("op") == 0 else None


def sync_profile_name(user, username=None, guild_nickname=None):
    # Keep a website nickname until Discord's effective display name changes.
    # Server nickname wins over global display name, which wins over username.
    old_name = user.guild_nickname or user.username
    new_name = guild_nickname or username or old_name
    source_known = bool(user.guild_nickname)
    if new_name and (
        not user.display_name
        or (new_name != old_name and (source_known or user.display_name == old_name))
    ):
        user.display_name = new_name
    if username:
        user.username = username
    if new_name and (guild_nickname or username):
        user.guild_nickname = new_name


def queue_sync(db, discord_id, now=None):
    from database import DiscordSyncPending, User

    discord_id = str(discord_id)
    if not db.query(User.id).filter(User.discord_id == discord_id).first():
        return False  # Never create profiles or queue the entire Discord guild.
    now = now or datetime.now()
    row = db.get(DiscordSyncPending, discord_id)
    if row is None:
        db.add(
            DiscordSyncPending(
                discord_id=discord_id, version=1, updated_at=now, retry_at=now
            )
        )
    else:
        row.version += 1
        row.updated_at = row.retry_at = now
    return True


async def gateway_notification(payload, guild_id):
    if not guild_id or not isinstance(payload, dict):
        return
    event, data = payload.get("t"), payload.get("d")
    if event not in {"GUILD_MEMBER_UPDATE", "GUILD_MEMBER_ADD", "GUILD_MEMBER_REMOVE"}:
        return
    if not isinstance(data, dict) or str(data.get("guild_id")) != str(guild_id):
        return
    user = data.get("user")
    if not isinstance(user, dict) or not user.get("id"):
        return
    from runtime import metadata_transaction

    async with metadata_transaction() as db:
        queue_sync(db, user["id"])


async def queue_known_users():
    from database import User
    from runtime import metadata_transaction

    async with metadata_transaction() as db:
        for (discord_id,) in db.query(User.discord_id).all():
            queue_sync(db, discord_id)


def member_events_available(flags):
    # Official Discord application flags: GATEWAY_GUILD_MEMBERS and LIMITED.
    return bool(int(flags) & ((1 << 14) | (1 << 15)))


async def verify_configuration(site):
    """Safe installation check: print role/intent status, never credentials."""
    if not all((site.REQUIRED_GUILD_ID, site.REQUIRED_ROLE_ID, site.DISCORD_BOT_TOKEN)):
        raise RuntimeError(
            "В .env нужны REQUIRED_GUILD_ID, REQUIRED_ROLE_ID и DISCORD_BOT_TOKEN"
        )
    async with site.get_discord_client(timeout=20) as client:
        headers = {"Authorization": f"Bot {site.DISCORD_BOT_TOKEN}"}
        roles = await client.get(
            site.get_discord_api_url(f"/guilds/{site.REQUIRED_GUILD_ID}/roles"),
            headers=headers,
        )
        if roles.status_code != 200:
            raise RuntimeError(
                "Не удалось проверить Discord-роли; повторите установку позже"
            )
        items = roles.json()
        if not isinstance(items, list):
            raise RuntimeError("Некорректный список ролей Discord")
        role = next(
            (
                r
                for r in items
                if isinstance(r, dict)
                and str(r.get("id")) == str(site.REQUIRED_ROLE_ID)
            ),
            None,
        )
        if not role or role.get("name", "").strip() != "Участник ST-9":
            raise RuntimeError(
                "REQUIRED_ROLE_ID должен указывать на роль «Участник ST-9». "
                "Проверьте ID роли в .env; токен менять не нужно"
            )
        info = await client.get(
            site.get_discord_api_url("/oauth2/applications/@me"), headers=headers
        )
        if info.status_code != 200:
            raise RuntimeError(
                "Не удалось проверить настройки бота; повторите установку позже"
            )
        payload = info.json()
        flags = payload.get("flags_new", payload.get("flags", 0))
        instant = member_events_available(flags)
    print("Required Discord role verified: Участник ST-9")
    print(
        "Server Members Intent: "
        + (
            "enabled; instant sync available"
            if instant
            else "disabled; 60-second fallback. Enable Bot > Privileged Gateway Intents > Server Members Intent and restart discord-bot.service"
        )
    )
    return instant


if __name__ == "__main__":
    import asyncio
    import main

    try:
        asyncio.run(verify_configuration(main))
    except Exception as exc:
        # HTTP transports can include request credentials in exception text.
        if isinstance(exc, RuntimeError):
            raise SystemExit(str(exc))
        raise SystemExit(
            "Не удалось проверить Discord. Проверьте соединение и повторите позже"
        )
