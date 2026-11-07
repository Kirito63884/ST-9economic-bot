"""Patch shared integration points without replacing unrelated server changes."""

from pathlib import Path
import sys


def add(source, block, anchor):
    if block in source:
        return source
    if source.count(anchor) != 1:
        raise ValueError("Неизвестная структура общего файла; файлы не изменены.")
    return source.replace(anchor, block + anchor)


def patch(app, stage):
    output = {}
    source = (app / "main.py").read_text()
    source = add(
        source,
        "from discord_admin_roles import sync_admin_roles, manual_role_change\n",
        "from medals import visible_awards, sync_manual\n",
    )
    for indent, anchor in (
        (
            "        ",
            "        user = ensure_discord_profile(db, discord_id, username, guild_nickname)\n",
        ),
        ("            ", "            link.used_at = link_utcnow()\n"),
    ):
        block = (
            indent
            + "if REQUIRED_GUILD_ID:\n"
            + indent
            + '    sync_admin_roles(db, user, membership.get("roles"))\n'
        )
        if anchor + block not in source:
            if source.count(anchor) != 1:
                raise ValueError("Неизвестный обработчик входа.")
            source = source.replace(anchor, anchor + block)
    anchor = "def apply_discord_membership(db, user, membership):\n"
    block = '    if REQUIRED_GUILD_ID:\n        sync_admin_roles(\n            db, user, membership.get("roles") if membership.get("allowed") else []\n        )\n'
    if anchor + block not in source:
        if source.count(anchor) != 1:
            raise ValueError("Неизвестный обработчик членства.")
        source = source.replace(anchor, anchor + block)
    source = add(
        source,
        "    manual_role_change(db, target)\n",
        '    log_game(db, uid, "admin", "role_change", 0, details)',
    )
    output["main.py"] = source
    source = (app / "roster_membership.py").read_text()
    block = """    from discord_admin_roles import sync_admin_roles

    verified_roles = {
        sid: member_roles
        if participant in member_roles and cargo not in member_roles
        else []
        for sid, _, _, member_roles in normalized
    }
    for sid, user in users.items():
        sync_admin_roles(db, user, verified_roles.get(sid, []))

"""
    source = add(
        source,
        block,
        "    health = db.get(RosterSyncHealth, 1)\n    if health is None:",
    )
    output["roster_membership.py"] = source
    source = (app / "automation_admin.py").read_text()
    block = """    from birthday_admin import make_router as make_birthday_router

    router.include_router(make_birthday_router(get_user, get_db, SECRET_KEY))

"""
    source = add(source, block, "    def chief(user):")
    output["automation_admin.py"] = source
    source = (app / "forum_events_admin.py").read_text()
    block = """    from forum_event_rewards import make_router as make_reward_router
    from main import SECRET_KEY

    router.include_router(make_reward_router(get_user, get_db, may_manage, SECRET_KEY))

"""
    output["forum_events_admin.py"] = add(source, block, "    def check(user):")
    source = (app / "templates/admin.html").read_text()
    source = add(
        source,
        '  <button class="nav-item" id="tab-birthdays" role="tab" aria-controls="panel-birthdays" aria-selected="false" tabindex="-1" onclick="switchTab(\'birthdays\')"><span class="nav-icon">🎂</span>Дни рождения</button>\n',
        '  {% if is_senior_or_super %}<button class="nav-item" id="tab-forum-events"',
    )
    source = add(
        source,
        '                {% include "_admin_birthdays.html" %}\n',
        '                {% if is_senior_or_super %}{% include "_admin_forum_events.html" %}{% endif %}',
    )
    source = add(
        source,
        '    if (name === "birthdays") window.loadBirthdays?.();\n',
        '    if (name === "forum-events")',
    )
    output["templates/admin.html"] = source
    source = (app / "templates/_admin_forum_events.html").read_text()
    output["templates/_admin_forum_events.html"] = add(
        source, '  {% include "_admin_event_rewards.html" %}\n', "</section>"
    )
    source = (app / "templates/_admin_forum_events.js.html").read_text()
    old = 'root.querySelectorAll("button, select, input, textarea").forEach(node => {node.disabled = value || node.dataset.readonly === "true";});'
    new = 'root.querySelectorAll("button, select, input, textarea").forEach(node => {if (!node.closest("#eventRewards")) node.disabled = value || node.dataset.readonly === "true";});'
    if new not in source:
        if source.count(old) != 1:
            raise ValueError("Неизвестный интерфейс событий.")
        source = source.replace(old, new)
    output["templates/_admin_forum_events.js.html"] = add(
        source, "    window.selectRewardEvent?.(event);\n", "    selected = event;"
    )
    source = (app / "discord_bot.py").read_text()
    output["discord_bot.py"] = add(
        source,
        "from discord_birthdays import register_birthdays\n\nregister_birthdays(bot)\n",
        "# ST9_SQUAD_BOT_END",
    )
    # All anchors were validated before any output file is changed.
    for name, source in output.items():
        dest = stage / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source)


if __name__ == "__main__":
    patch(Path(sys.argv[1]), Path(sys.argv[2]))
