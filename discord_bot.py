import os
import logging
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(Path(__file__).resolve().parent / ".env")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
REQUIRED_GUILD_ID = os.getenv("REQUIRED_GUILD_ID")
REQUIRED_ROLE_ID = os.getenv("REQUIRED_ROLE_ID")
ADMIN_DISCORD_ID = os.getenv("ADMIN_DISCORD_ID")
logger = logging.getLogger(__name__)

from database import SessionLocal, User, GameHistory
from runtime import initialize_database, metadata_transaction
from login_links import LoginLinkError, issue_login_link
from sqlalchemy.exc import IntegrityError


# === Бот ===
intents = discord.Intents.default()
intents.voice_states = True


class SyncedBot(commands.Bot):
    async def setup_hook(self):
        from discord_specialties import SpecialtyView
        from discord_sync import member_events_available

        self.add_view(SpecialtyView(self.st9_specialties))

        # ConnectionState retains this Intents object; configure it before IDENTIFY.
        # Raw gateway notifications avoid relying on the member cache.
        intents.members = member_events_available(self.application.flags.value)
        if intents.members:
            logger.warning(
                "Discord member events enabled: nickname and role sync active"
            )
        else:
            logger.warning(
                "Server Members Intent is disabled in Discord Developer Portal; "
                "website nickname/role checks use the 60-second fallback"
            )


bot = SyncedBot(
    command_prefix="!",
    intents=intents,
    enable_debug_events=True,
    chunk_guilds_at_startup=False,
    member_cache_flags=discord.MemberCacheFlags.none(),
)


@bot.event
async def on_socket_raw_receive(raw):
    from discord_sync import gateway_notification, gateway_packet

    try:
        await gateway_notification(gateway_packet(raw), REQUIRED_GUILD_ID)
    except Exception:
        logger.exception(
            "Cannot queue Discord membership change; periodic check will retry"
        )


async def reply(interaction, *args, **kwargs):
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


def can_view_profile(db, interaction, user):
    if user.profile_public is not False or user.discord_id == str(interaction.user.id):
        return True
    viewer = (
        db.query(User)
        .filter(User.discord_id == str(interaction.user.id), User.deleted_at.is_(None))
        .first()
    )
    return bool(viewer and viewer.is_admin)


async def verify_interaction(interaction):
    if REQUIRED_GUILD_ID and str(interaction.guild_id) != REQUIRED_GUILD_ID:
        await reply(
            interaction,
            "Команды доступны только на нужном Discord-сервере",
            ephemeral=True,
        )
        return False
    if REQUIRED_ROLE_ID and str(interaction.user.id) != ADMIN_DISCORD_ID:
        role_ids = {str(role.id) for role in getattr(interaction.user, "roles", [])}
        if REQUIRED_ROLE_ID not in role_ids:
            await reply(
                interaction, "Для доступа нужна требуемая Discord-роль", ephemeral=True
            )
            return False
    return True


bot.tree.interaction_check = verify_interaction


# === Вспомогательные функции ===
def validate_steam_id(steam_id: str) -> bool:
    """Проверка Steam ID (SteamID64 - 17 цифр, начинается с 7656)"""
    if not steam_id:
        return False
    clean = steam_id.strip()
    return (
        clean.isascii()
        and clean.isdigit()
        and len(clean) == 17
        and clean.startswith("7656")
    )


def format_time_ago(dt):
    if not dt:
        return "Никогда"
    diff = (datetime.now() - dt).total_seconds()
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{int(diff / 60)} мин назад"
    if diff < 86400:
        return f"{int(diff / 3600)} ч назад"
    return f"{int(diff / 86400)} дн назад"


def is_online(user):
    if not user.last_seen:
        return False
    return (datetime.now() - user.last_seen).total_seconds() < 300


GAME_ICONS = {
    "blackjack": "♠️",
    "roulette": "🎡",
    "poker": "🃏",
    "crash": "📈",
    "durak": "🂡",
}

GAME_NAMES = {
    "blackjack": "Блэкджек",
    "roulette": "Рулетка",
    "poker": "Покер",
    "crash": "Crash",
    "durak": "Дурак",
}


# === Событие запуска ===
@bot.event
async def on_ready():
    from discord_sync import queue_known_users

    if REQUIRED_GUILD_ID:
        try:
            await queue_known_users()
        except Exception:
            logger.exception("Cannot queue Discord reconnect verification")
    print("=" * 60, flush=True)
    print(f"🚀 БОТ ЗАПУЩЕН: {bot.user.name} (ID: {bot.user.id})", flush=True)
    print("=" * 60, flush=True)

    guild_id = os.getenv("REQUIRED_GUILD_ID")
    if guild_id:
        try:
            guild_obj = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(
                f"✅ Синхронизировано {len(synced)} команд для сервера {guild_id}",
                flush=True,
            )
            for cmd in synced:
                print(f"   → /{cmd.name}", flush=True)
        except Exception as e:
            print(f"❌ Ошибка синхронизации: {e}", flush=True)
            import traceback

            traceback.print_exc()
    else:
        try:
            synced = await bot.tree.sync()
            print(f"✅ Глобально синхронизировано {len(synced)} команд", flush=True)
        except Exception as e:
            print(f"❌ Ошибка: {e}", flush=True)

    print("=" * 60, flush=True)


# === Глобальный обработчик ошибок ===
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
):
    logger.error(
        "Command failed: %s",
        getattr(interaction.command, "name", "unknown"),
        exc_info=error,
    )
    import traceback

    traceback.print_exc()

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Ошибка выполнения команды. Подробности сохранены в серверном журнале.",
                ephemeral=True,
            )
        else:
            await reply(
                interaction,
                "❌ Ошибка выполнения команды. Подробности сохранены в серверном журнале.",
                ephemeral=True,
            )
    except Exception as e:
        print(f"⚠️ Не удалось отправить сообщение об ошибке: {e}", flush=True)


# === БАЗОВЫЕ КОМАНДЫ ===
@bot.tree.command(name="login", description="🔑 Войти на сайт без Discord в браузере")
async def login_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild or (
        REQUIRED_GUILD_ID and str(interaction.guild_id) != REQUIRED_GUILD_ID
    ):
        await reply(
            interaction, "Используйте /login на нужном Discord-сервере", ephemeral=True
        )
        return
    try:
        # Fetch fresh roles, rather than trusting an old cached membership.
        member = await interaction.guild.fetch_member(interaction.user.id)
        if REQUIRED_ROLE_ID and REQUIRED_ROLE_ID not in {
            str(role.id) for role in member.roles
        }:
            await reply(
                interaction, "Для входа нужна требуемая Discord-роль", ephemeral=True
            )
            return
        async with metadata_transaction() as db:
            _, url = issue_login_link(
                db, str(interaction.user.id), "bot", str(interaction.user.id)
            )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Войти на сайт", style=discord.ButtonStyle.link, url=url
            )
        )
        await reply(
            interaction,
            "🔑 Ссылка для входа в ваш профиль:\n"
            + url
            + "\n\nДействует 10 минут и только один раз. Можно скопировать её на компьютер. "
            "Откройте ссылку и нажмите «Войти в мой профиль». Никому её не передавайте. "
            "Новая ссылка отменяет предыдущую.",
            view=view,
            ephemeral=True,
        )
    except discord.NotFound:
        await reply(
            interaction, "Вы больше не состоите на Discord-сервере", ephemeral=True
        )
    except discord.HTTPException:
        await reply(
            interaction,
            "Discord временно не позволяет проверить роль. Попробуйте позже.",
            ephemeral=True,
        )
    except LoginLinkError as exc:
        await reply(interaction, str(exc), ephemeral=True)


@bot.tree.command(name="top", description="🏆 Топ-10 игроков по балансу")
async def top_command(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        top_users = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .order_by(User.balance.desc())
            .limit(10)
            .all()
        )

        if not top_users:
            await reply(interaction, "❌ Пока нет игроков", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, u in enumerate(top_users):
            medal = medals[i] if i < 3 else f"**{i + 1}.**"
            name = discord.utils.escape_markdown(u.display_name or u.username)[:100]
            admin_badge = " 👑" if u.is_admin else ""
            online = "🟢" if is_online(u) else "⚪"
            lines.append(
                f"{medal} {online} **{name}**{admin_badge} — 💵 **{u.balance:,.2f}**"
            )

        embed = discord.Embed(
            title="🏆 Топ-10 игроков", description="\n".join(lines), color=0xFFD700
        )
        embed.set_footer(
            text=f"Всего игроков: {db.query(User).filter(User.deleted_at.is_(None)).count()}"
        )
        await reply(interaction, embed=embed)
    finally:
        db.close()


@bot.tree.command(name="players", description="👥 Список всех игроков")
async def players_command(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        users = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .order_by(User.balance.desc())
            .all()
        )

        if not users:
            await reply(interaction, "❌ Пока нет игроков", ephemeral=True)
            return

        page_size = 20
        total_pages = (len(users) + page_size - 1) // page_size

        embeds = []
        for page in range(total_pages):
            start = page * page_size
            end = min(start + page_size, len(users))
            page_users = users[start:end]

            lines = []
            for i, u in enumerate(page_users, start=start + 1):
                name = discord.utils.escape_markdown(u.display_name or u.username)[:100]
                admin_badge = " 👑" if u.is_admin else ""
                online = "🟢" if is_online(u) else "⚪"
                lines.append(
                    f"**{i}.** {online} **{name}**{admin_badge} — 💵 {u.balance:,.2f}"
                )

            embed = discord.Embed(
                title=f"👥 Все игроки (стр. {page + 1}/{total_pages})",
                description="\n".join(lines),
                color=0x667EEA,
            )
            embed.set_footer(
                text=f"Всего: {len(users)} | 🟢 онлайн, ⚪ оффлайн, 👑 админ"
            )
            embeds.append(embed)

        if len(embeds) == 1:
            await reply(interaction, embed=embeds[0])
        else:
            await reply(interaction, embed=embeds[0])
            for embed in embeds[1:10]:
                await interaction.followup.send(embed=embed)
            if len(embeds) > 10:
                await interaction.followup.send(
                    f"📄 Показано 10 из {len(embeds)} страниц. Используйте `/top` для быстрого просмотра."
                )
    finally:
        db.close()


@bot.tree.command(name="online", description="🟢 Кто сейчас онлайн")
async def online_command(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        threshold = datetime.now() - timedelta(minutes=5)
        online_users = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.last_seen > threshold)
            .order_by(User.last_seen.desc())
            .all()
        )

        if not online_users:
            embed = discord.Embed(
                title="🟢 Онлайн", description="Никого нет онлайн 😴", color=0x757E8A
            )
            await reply(interaction, embed=embed)
            return

        lines = []
        for u in online_users[:20]:
            name = discord.utils.escape_markdown(u.display_name or u.username)[:100]
            admin_badge = " 👑" if u.is_admin else ""
            time_ago = format_time_ago(u.last_seen)
            fav = GAME_ICONS.get(u.favorite_game, "") if u.favorite_game else ""
            lines.append(f"🟢 **{name}**{admin_badge} {fav} — _{time_ago}_")

        embed = discord.Embed(
            title=f"🟢 Онлайн ({len(online_users)})",
            description="\n".join(lines),
            color=0x4CAF50,
        )
        embed.set_footer(text="Обновляется при активности на сайте")
        await reply(interaction, embed=embed)
    finally:
        db.close()


@bot.tree.command(name="me", description="📊 Моя статистика")
async def me_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.discord_id == discord_id)
            .first()
        )

        if not user:
            embed = discord.Embed(
                title="❌ Вы не зарегистрированы",
                description="Зайдите на сайт через Discord, чтобы создать аккаунт",
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        await send_profile_embed(interaction, user, is_self=True)
    finally:
        db.close()


@bot.tree.command(name="balance", description="💵 Баланс игрока")
@app_commands.describe(target="Игрок (упоминание или ник)")
async def balance_command(interaction: discord.Interaction, target: str = None):
    db = SessionLocal()
    try:
        if target:
            if target.startswith("<@") and target.endswith(">"):
                mentioned_id = target.strip("<@!>")
                user = (
                    db.query(User)
                    .filter(User.deleted_at.is_(None))
                    .filter(User.discord_id == mentioned_id)
                    .first()
                )
            else:
                user = (
                    db.query(User)
                    .filter(User.deleted_at.is_(None))
                    .filter(
                        (User.display_name.ilike(f"%{target}%"))
                        | (User.username.ilike(f"%{target}%"))
                    )
                    .first()
                )
        else:
            discord_id = str(interaction.user.id)
            user = (
                db.query(User)
                .filter(User.deleted_at.is_(None))
                .filter(User.discord_id == discord_id)
                .first()
            )

        if not user:
            await reply(interaction, "❌ Игрок не найден", ephemeral=True)
            return

        name = discord.utils.escape_markdown(user.display_name or user.username)[:100]
        admin_badge = " 👑" if user.is_admin else ""
        online = "🟢 Онлайн" if is_online(user) else "⚪ Оффлайн"
        position = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.balance > user.balance)
            .count()
            + 1
        )

        embed = discord.Embed(
            title=f"💵 Баланс: {name}{admin_badge}"[:256], color=0xFFD700
        )
        embed.add_field(name="💰 Баланс", value=f"**{user.balance:,.2f}**", inline=True)
        embed.add_field(name="🏆 Место", value=f"**#{position}**", inline=True)
        embed.add_field(name="📡 Статус", value=online, inline=True)
        profile_visible = can_view_profile(db, interaction, user)
        if profile_visible and user.guild_nickname and user.guild_nickname != name:
            embed.add_field(
                name="🎮 Ник на сервере", value=user.guild_nickname[:1024], inline=False
            )
        if profile_visible and user.steam_id:
            embed.add_field(
                name="🎮 Steam ID", value=f"`{user.steam_id}`", inline=False
            )

        await reply(interaction, embed=embed)
    finally:
        db.close()


@bot.tree.command(name="profile", description="👤 Полный профиль игрока")
@app_commands.describe(target="Игрок (упоминание или ник)")
async def profile_command(interaction: discord.Interaction, target: str = None):
    db = SessionLocal()
    try:
        if target:
            if target.startswith("<@") and target.endswith(">"):
                mentioned_id = target.strip("<@!>")
                user = (
                    db.query(User)
                    .filter(User.deleted_at.is_(None))
                    .filter(User.discord_id == mentioned_id)
                    .first()
                )
            else:
                user = (
                    db.query(User)
                    .filter(User.deleted_at.is_(None))
                    .filter(
                        (User.display_name.ilike(f"%{target}%"))
                        | (User.username.ilike(f"%{target}%"))
                    )
                    .first()
                )
        else:
            discord_id = str(interaction.user.id)
            user = (
                db.query(User)
                .filter(User.deleted_at.is_(None))
                .filter(User.discord_id == discord_id)
                .first()
            )

        if not user:
            await reply(interaction, "❌ Игрок не найден", ephemeral=True)
            return

        is_self = str(interaction.user.id) == user.discord_id
        await send_profile_embed(interaction, user, is_self=is_self)
    finally:
        db.close()


async def send_profile_embed(interaction, user, is_self=False):
    with SessionLocal() as access_db:
        viewer = (
            access_db.query(User)
            .filter(
                User.discord_id == str(interaction.user.id), User.deleted_at.is_(None)
            )
            .first()
        )
        if (
            user.profile_public is False
            and not is_self
            and not (viewer and viewer.is_admin)
        ):
            await reply(interaction, "🔒 Профиль скрыт", ephemeral=True)
            return
    db = SessionLocal()
    try:
        name = discord.utils.escape_markdown(user.display_name or user.username)[:100]
        admin_badge = " 👑" if user.is_admin else ""
        online = "🟢 Онлайн" if is_online(user) else "⚪ Оффлайн"
        position = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.balance > user.balance)
            .count()
            + 1
        )

        try:
            theme_color = int((user.theme_color or "#667eea").lstrip("#"), 16)
        except Exception:
            theme_color = 0x667EEA

        embed = discord.Embed(
            title=f"{user.avatar_emoji or '👤'} {name}{admin_badge}"[:256],
            color=max(0, min(theme_color, 0xFFFFFF)),
        )

        if user.bio:
            embed.add_field(name="📝 О себе", value=user.bio[:500], inline=False)

        if user.quote:
            embed.add_field(name="💬 Цитата", value=f"_{user.quote}_", inline=False)

        embed.add_field(name="💰 Баланс", value=f"**{user.balance:,.2f}**", inline=True)
        embed.add_field(name="🏆 Место", value=f"**#{position}**", inline=True)
        embed.add_field(name="📡 Статус", value=online, inline=True)

        if user.favorite_game:
            icon = GAME_ICONS.get(user.favorite_game, "🎮")
            gname = GAME_NAMES.get(user.favorite_game, user.favorite_game)
            embed.add_field(
                name="🎮 Любимая игра", value=f"{icon} {gname}", inline=True
            )

        if user.daily_streak and user.daily_streak > 0:
            embed.add_field(
                name="🔥 Стрик", value=f"**{user.daily_streak} дн**", inline=True
            )

        if user.guild_nickname and user.guild_nickname != name:
            embed.add_field(
                name="🎮 Ник на сервере", value=user.guild_nickname[:1024], inline=True
            )

        if user.steam_id:
            embed.add_field(
                name="🎮 Steam ID", value=f"`{user.steam_id}`", inline=False
            )
            embed.add_field(
                name="🔗 Профиль Steam",
                value=f"[Открыть](https://steamcommunity.com/profiles/{user.steam_id})",
                inline=False,
            )

        embed.set_footer(text=f"ID: {user.id} | @{user.username}")

        if is_self:
            await reply(interaction, embed=embed, ephemeral=True)
        else:
            await reply(interaction, embed=embed)
    finally:
        db.close()


# === STEAM КОМАНДЫ ===
@bot.tree.command(name="steam", description="🎮 Привязать свой Steam ID (17 цифр)")
@app_commands.describe(steam_id="Steam ID (SteamID64, 17 цифр, начинается с 7656)")
async def steam_command(interaction: discord.Interaction, steam_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        discord_id = str(interaction.user.id)

        if not validate_steam_id(steam_id):
            embed = discord.Embed(
                title="❌ Неверный Steam ID",
                description=(
                    "Steam ID должен быть:\n"
                    "• 17 цифр\n"
                    "• Начинаться с `7656`\n\n"
                    "**Как узнать:**\n"
                    "1. Откройте [steamid.io](https://steamid.io)\n"
                    "2. Вставьте ссылку на профиль\n"
                    "3. Скопируйте `steamID64`"
                ),
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        db = SessionLocal()
        try:
            user = (
                db.query(User)
                .filter(User.deleted_at.is_(None))
                .filter(User.discord_id == discord_id)
                .first()
            )
            if not user:
                embed = discord.Embed(
                    title="❌ Вы не зарегистрированы",
                    description="Зайдите на сайт через Discord, чтобы создать аккаунт",
                    color=0xF44336,
                )
                await reply(interaction, embed=embed, ephemeral=True)
                return

            existing = (
                db.query(User)
                .filter(User.steam_id == steam_id.strip(), User.id != user.id)
                .first()
            )
            if existing:
                embed = discord.Embed(
                    title="❌ Steam ID уже используется",
                    description=f"Этот Steam ID уже привязан к пользователю **{existing.display_name or existing.username}**",
                    color=0xF44336,
                )
                await reply(interaction, embed=embed, ephemeral=True)
                return

            old_steam = user.steam_id
            user.steam_id = steam_id.strip()
            db.add(
                GameHistory(
                    user_id=user.id,
                    game_type="admin",
                    result="steam_set",
                    amount=0,
                    details="Steam ID привязан через Discord-бота",
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                await reply(
                    interaction, "Этот Steam ID уже используется", ephemeral=True
                )
                return

            embed = discord.Embed(title="✅ Steam ID привязан", color=0x4CAF50)
            if old_steam:
                embed.add_field(
                    name="Старый Steam ID", value=f"`{old_steam}`", inline=False
                )
            embed.add_field(
                name="Новый Steam ID", value=f"`{user.steam_id}`", inline=False
            )
            embed.add_field(
                name="Профиль",
                value=f"[steamcommunity.com/profiles/{user.steam_id}](https://steamcommunity.com/profiles/{user.steam_id})",
                inline=False,
            )
            embed.set_footer(
                text="Теперь админы смогут начислять вам деньги по Steam ID"
            )

            await reply(interaction, embed=embed, ephemeral=True)
        finally:
            db.close()
    except Exception as e:
        print(f"❌ Ошибка в /steam: {e}", flush=True)
        import traceback

        traceback.print_exc()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Не удалось выполнить команду. Попробуйте позже.", ephemeral=True
                )
            else:
                await reply(
                    interaction,
                    "❌ Не удалось выполнить команду. Попробуйте позже.",
                    ephemeral=True,
                )
        except Exception:
            pass


@bot.tree.command(
    name="steam-set", description="👑 Привязать Steam ID игроку (только админы)"
)
@app_commands.describe(
    target="Игрок (упоминание)", steam_id="Steam ID (17 цифр, начинается с 7656)"
)
async def steam_set_command(
    interaction: discord.Interaction, target: discord.User, steam_id: str
):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    db = SessionLocal()
    try:
        admin = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.discord_id == discord_id)
            .first()
        )
        if not admin or not admin.is_admin:
            embed = discord.Embed(
                title="❌ Нет прав",
                description="Эта команда доступна только администраторам",
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        if not validate_steam_id(steam_id):
            embed = discord.Embed(
                title="❌ Неверный Steam ID",
                description="Steam ID должен быть 17 цифр и начинаться с `7656`",
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        target_discord_id = str(target.id)
        target_user = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.discord_id == target_discord_id)
            .first()
        )
        if not target_user:
            embed = discord.Embed(
                title="❌ Пользователь не найден",
                description=f"Игрок {target.mention} не зарегистрирован на сайте",
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        existing = (
            db.query(User)
            .filter(User.steam_id == steam_id.strip(), User.id != target_user.id)
            .first()
        )
        if existing:
            embed = discord.Embed(
                title="❌ Steam ID уже используется",
                description=f"Этот Steam ID уже привязан к **{existing.display_name or existing.username}**",
                color=0xF44336,
            )
            await reply(interaction, embed=embed, ephemeral=True)
            return

        old_steam = target_user.steam_id
        target_user.steam_id = steam_id.strip()
        db.add(
            GameHistory(
                user_id=target_user.id,
                game_type="admin",
                result="steam_set",
                amount=0,
                details=f"Steam ID привязан Discord-администратором {admin.username}",
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            await reply(interaction, "Этот Steam ID уже используется", ephemeral=True)
            return

        embed = discord.Embed(title="✅ Steam ID привязан", color=0x4CAF50)
        embed.add_field(name="Игрок", value=target.mention, inline=False)
        if old_steam:
            embed.add_field(
                name="Старый Steam ID", value=f"`{old_steam}`", inline=False
            )
        embed.add_field(
            name="Новый Steam ID", value=f"`{target_user.steam_id}`", inline=False
        )
        embed.add_field(
            name="Привязал",
            value=f"{admin.display_name or admin.username}",
            inline=False,
        )

        await reply(interaction, embed=embed)
    finally:
        db.close()


@bot.tree.command(name="steam-lookup", description="🔍 Найти игрока по Steam ID")
@app_commands.describe(steam_id="Steam ID для поиска")
async def steam_lookup_command(interaction: discord.Interaction, steam_id: str):
    if not validate_steam_id(steam_id):
        embed = discord.Embed(
            title="❌ Неверный Steam ID",
            description="Steam ID должен быть 17 цифр и начинаться с `7656`",
            color=0xF44336,
        )
        await reply(interaction, embed=embed, ephemeral=True)
        return

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.deleted_at.is_(None))
            .filter(User.steam_id == steam_id.strip())
            .first()
        )
        if not user:
            embed = discord.Embed(
                title="🔍 Не найдено",
                description=f"Игрок с Steam ID `{steam_id}` не зарегистрирован на сайте",
                color=0xFF9800,
            )
            await reply(interaction, embed=embed)
            return

        if not can_view_profile(db, interaction, user):
            await reply(interaction, "🔒 Профиль скрыт", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎮 {user.display_name or user.username}", color=0x4CAF50
        )
        embed.add_field(name="Steam ID", value=f"`{user.steam_id}`", inline=False)
        embed.add_field(
            name="Профиль Steam",
            value=f"[Открыть](https://steamcommunity.com/profiles/{user.steam_id})",
            inline=True,
        )
        embed.add_field(name="Баланс", value=f"{user.balance:.2f}", inline=True)
        embed.add_field(
            name="Админ", value="Да" if user.is_admin else "Нет", inline=True
        )
        if user.guild_nickname:
            embed.add_field(
                name="Ник на сервере", value=user.guild_nickname[:1024], inline=False
            )

        await reply(interaction, embed=embed)
    finally:
        db.close()


# === HELP ===
@bot.tree.command(name="help", description="❓ Список команд")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="❓ Команды бота",
        description="Все команды для работы с сайтом",
        color=0x667EEA,
    )
    embed.add_field(name="/top", value="🏆 Топ-10 игроков по балансу", inline=False)
    embed.add_field(name="/players", value="👥 Список всех игроков", inline=False)
    embed.add_field(name="/online", value="🟢 Кто сейчас онлайн", inline=False)
    embed.add_field(name="/me", value="📊 Моя статистика", inline=False)
    embed.add_field(name="/balance [@user]", value="💵 Баланс игрока", inline=False)
    embed.add_field(name="/profile [@user]", value="👤 Полный профиль", inline=False)
    embed.add_field(
        name="/login", value="🔑 Вход на сайт без Discord в браузере", inline=False
    )
    embed.add_field(
        name="/steam <id>", value="🎮 Привязать свой Steam ID", inline=False
    )
    embed.add_field(
        name="/steam-set <@user> <id>",
        value="👑 Привязать Steam ID игроку",
        inline=False,
    )
    embed.add_field(
        name="/steam-lookup <id>", value="🔍 Найти по Steam ID", inline=False
    )
    embed.add_field(name="/squad", value="🎮 Участники на серверах Squad", inline=False)
    embed.set_footer(text="💡 Совет: используйте /profile для детальной информации")
    await reply(interaction, embed=embed, ephemeral=True)


# ST9_SQUAD_BOT_BEGIN
from squad_bot import register_squad

register_squad(bot, reply)
from medals_bot import register_medals

register_medals(bot)
from discord_event_log import register_discord_event_log

register_discord_event_log(bot)
from assembly_attendance import register_assembly

register_assembly(bot)
from roster_membership import register_roster_sync

register_roster_sync(bot)
from discord_roster import register_roster_publication

register_roster_publication(bot)
from group_analytics import GroupRosterSnapshot  # noqa: F401
from discord_specialties import register_specialties

register_specialties(bot)
from assembly_reports import register_assembly_reports

register_assembly_reports(bot)
# ST9_SQUAD_BOT_END

# === Запуск ===
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN не задан в .env")
    initialize_database()
    print("🚀 Запуск Discord-бота...")
    bot.run(DISCORD_BOT_TOKEN)
