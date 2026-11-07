from pathlib import Path

import pytest

from scripts.patch_admin_features import patch

ROOT = Path(__file__).resolve().parents[1]
SHARED = (
    "main.py",
    "roster_membership.py",
    "automation_admin.py",
    "forum_events_admin.py",
    "discord_bot.py",
    "templates/admin.html",
    "templates/_admin_forum_events.html",
    "templates/_admin_forum_events.js.html",
)


BASE = {
    "forum_events_admin.py": "    def check(user):\n        pass\n",
    "main.py": 'from medals import visible_awards, sync_manual\n        user = ensure_discord_profile(db, discord_id, username, guild_nickname)\n            link.used_at = link_utcnow()\ndef apply_discord_membership(db, user, membership):\n    pass\n    log_game(db, uid, "admin", "role_change", 0, details)\n',
    "roster_membership.py": "    health = db.get(RosterSyncHealth, 1)\n    if health is None:\n        pass\n",
    "automation_admin.py": "    def chief(user):\n        pass\n",
    "discord_bot.py": "register_forum_events(bot)\n# ST9_SQUAD_BOT_END\n",
    "templates/admin.html": '  {% if is_senior_or_super %}<button class="nav-item" id="tab-forum-events"\n                {% if is_senior_or_super %}{% include "_admin_forum_events.html" %}{% endif %}\n    if (name === "forum-events") window.loadForumEvents?.();\n',
    "templates/_admin_forum_events.html": "</section>",
    "templates/_admin_forum_events.js.html": '    selected = event;\nroot.querySelectorAll("button, select, input, textarea").forEach(node => {node.disabled = value || node.dataset.readonly === "true";});\n',
}


def test_patch_old_installation_preserves_features_and_repeats(tmp_path):
    app, stage = tmp_path / "app", tmp_path / "stage"
    for name in SHARED:
        dest = app / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(BASE[name])
    bot = app / "discord_bot.py"
    bot.write_text(bot.read_text() + "\nLOCAL_FEATURE = True\n")
    patch(app, stage)
    assert "LOCAL_FEATURE = True" in (stage / "discord_bot.py").read_text()
    before = {name: (stage / name).read_text() for name in SHARED}
    assert "register_forum_events(bot)" in before["discord_bot.py"]
    patch(stage, stage)
    assert before == {name: (stage / name).read_text() for name in SHARED}
    (app / "automation_admin.py").write_text("unknown layout")
    with pytest.raises(ValueError):
        patch(app, stage)
    assert before == {name: (stage / name).read_text() for name in SHARED}


def test_actual_installer_probe():
    source = (ROOT / "scripts/update_admin_features.sh").read_text()
    code = source.split("runuser -u www-data -- ./venv/bin/python - <<'PY'\n", 1)[
        1
    ].split("\nPY\n", 1)[0]
    exec(compile(code, "admin-installer-probe", "exec"), {})
