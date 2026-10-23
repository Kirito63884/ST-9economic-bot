"""Discord /squad uses the website snapshot; no token or external API needed."""

import asyncio
import discord
from database import SessionLocal
from squad_routes import public_roster


def roster_pages(document):
    pages = []
    for server in document["servers"]:
        if server["stale"] or server["status"] != "ready":
            warning = "⚠️ Данные устарели или недоступны; список ниже не подтверждает текущий онлайн."
        elif server["pending"]:
            warning = f"⚠️ Проверяем Steam ID ещё {server['pending']} игроков. Список может быть неполным."
        else:
            warning = "Участники Strike Team 9 по данным сайта."
        escape = lambda value: discord.utils.escape_markdown(str(value)).replace(
            "@", "@\u200b"
        )
        lines = [
            f"• **{escape(u['name'])}** · Discord: {escape(u['discord_name'])}"
            for u in server["members"]
        ]
        lines = lines or ["Участники не найдены."]
        prefix = warning + "\n\n"
        chunks, current = [], prefix
        for line in lines:
            line = line[:700]
            if len(current) + len(line) + 1 > 3600:
                chunks.append(current)
                current = prefix
            current += line + "\n"
        chunks.append(current)
        for chunk in chunks:
            pages.append(
                {
                    "title": server["name"][:240],
                    "description": chunk,
                    "url": server["url"],
                }
            )
    return pages


def read_pages():
    with SessionLocal() as db:
        return roster_pages(public_roster(db))


class SquadPages(discord.ui.View):
    def __init__(self, pages, viewer_id):
        super().__init__(timeout=180)
        self.pages, self.viewer_id, self.index = pages, viewer_id, 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1

    def embed(self):
        page = self.pages[self.index]
        embed = discord.Embed(
            title=page["title"],
            description=page["description"],
            url=page["url"],
            color=0x6595FF,
        )
        embed.set_footer(
            text=f"Страница {self.index + 1}/{len(self.pages)} · Снимок на момент команды · актуальный список: striketeam9bank.ru/squad"
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.viewer_id:
            return True
        await interaction.response.send_message(
            "Вызовите /squad, чтобы листать свой список.", ephemeral=True
        )
        return False

    @discord.ui.button(label="← Назад", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.index = max(0, self.index - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Далее →", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, button):
        await interaction.response.defer()
        self.pages = await asyncio.to_thread(read_pages)
        self.index = min(self.index, len(self.pages) - 1)
        self.update_buttons()
        await interaction.edit_original_response(embed=self.embed(), view=self)


def register_squad(bot, reply):
    @bot.tree.command(
        name="squad", description="🎮 Участники Strike Team 9 на серверах Squad"
    )
    async def squad_command(interaction: discord.Interaction):
        await interaction.response.defer()
        pages = await asyncio.to_thread(read_pages)
        view = SquadPages(pages, interaction.user.id)
        await reply(interaction, embed=view.embed(), view=view)
