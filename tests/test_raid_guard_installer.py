from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_raid_installer_patch_preserves_hooks_and_repeats(tmp_path):
    script = (ROOT / "scripts/update_raid_guard.sh").read_text()
    code = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    source, target = tmp_path / "installed.py", tmp_path / "new.py"
    source.write_text(
        "register_birthdays(bot)\nregister_forum_events(bot)\nLOCAL_FEATURE = True\n# ST9_SQUAD_BOT_END\n"
    )
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-", str(source), str(target)],
            input=code,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert target.read_text().count("register_raid_guard(bot)") == 1
        assert "register_birthdays(bot)" in target.read_text()
        assert "register_forum_events(bot)" in target.read_text()
        assert "LOCAL_FEATURE = True" in target.read_text()
        source.write_text(target.read_text())
    before = target.read_text()
    source.write_text("UNKNOWN_STRUCTURE = True\n")
    result = subprocess.run(
        [sys.executable, "-", str(source), str(target)],
        input=code,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert target.read_text() == before


def test_real_bot_registers_raid_guard_and_previous_features_without_login():
    import discord_bot

    bot = discord_bot.bot
    assert bot.st9_raid_guard.joined in bot.extra_events["on_member_join"]
    assert bot.st9_raid_guard.ready in bot.extra_events["on_ready"]
    assert bot.st9_raid_guard.ready in bot.extra_events["on_resumed"]
    for name in ("raid_off", "raid_status", "raid_delay", "event_create", "add_bd"):
        assert bot.tree.get_command(name)
    assert bot.st9_raid_guard.task is None
