"""Patch only integration hooks in installed shared files; preserve other features."""

from pathlib import Path
import sys


def add(source, block, anchor, before=True):
    if block in source:
        return source
    if source.count(anchor) != 1:
        raise ValueError("Неизвестная структура общего файла; обновление остановлено.")
    return source.replace(anchor, block + anchor if before else anchor + block)


def patch(app, stage):
    outputs = {}
    source = (app / "discord_bot.py").read_text()
    source = add(
        source,
        "from discord_forum_events import register_forum_events\n\nregister_forum_events(bot)\n",
        "# ST9_SQUAD_BOT_END",
    )
    source = add(
        source,
        "\n        self.st9_forum_events.restore_views()",
        "        self.add_view(SpecialtyView(self.st9_specialties))",
        before=False,
    )
    outputs["discord_bot.py"] = source
    source = (app / "automation_admin.py").read_text()
    source = add(
        source,
        "    from forum_events_admin import make_router as make_forum_events_router\n\n    router.include_router(make_forum_events_router(get_user, get_db, is_senior_or_super))\n\n",
        "    def chief(user):",
    )
    outputs["automation_admin.py"] = source
    source = (app / "templates/admin.html").read_text()
    source = add(
        source,
        '  {% if is_senior_or_super %}<button class="nav-item" id="tab-forum-events" role="tab" aria-controls="panel-forum-events" aria-selected="false" tabindex="-1" onclick="switchTab(\'forum-events\')"><span class="nav-icon">◷</span>События</button>{% endif %}\n',
        '  {% if is_senior_or_super %}<button class="nav-item" id="tab-roster-settings"',
    )
    source = add(
        source,
        '                {% if is_senior_or_super %}{% include "_admin_forum_events.html" %}{% endif %}\n',
        '                {% if is_senior_or_super %}{% include "_admin_roster_settings.html" %}{% endif %}',
    )
    source = add(
        source,
        '    if (name === "forum-events") window.loadForumEvents?.();\n',
        '    if (name === "roster-settings")',
    )
    outputs["templates/admin.html"] = source
    source = (app / "templates/_site_nav.html").read_text()
    source = add(
        source,
        '            {% if user %}<a href="/calendar" {% if page_path == \'/calendar\' %}aria-current="page"{% endif %}>Календарь</a>{% endif %}\n',
        '            <a href="/games"',
    )
    source = add(
        source,
        '<a href="/calendar">Мой календарь</a>',
        '<a href="/profile">Мой профиль</a>',
        before=False,
    )
    outputs["templates/_site_nav.html"] = source
    # Validate all anchors before writing any staged files.
    for name, contents in outputs.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)


if __name__ == "__main__":
    patch(Path(sys.argv[1]), Path(sys.argv[2]))
