"""Upgrade preflight covers Protocol and games before any service is stopped."""

from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/update_games_and_access.sh").read_text()
FILES = SCRIPT.split("files=(\n", 1)[1].split("\n)", 1)[0].split()


def test_upgrade_preflight_includes_web_login_and_new_games(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    for source in ROOT.glob("*.py"):
        if source.name not in {
            "protocol_login.py",
            "protocol_login_admin.py",
            "horse_racing.py",
            "horse_racing_routes.py",
        }:
            shutil.copy(source, target / source.name)
    shutil.copytree(ROOT / "templates", target / "templates")
    (target / "templates/_protocol_login.html").unlink()
    for template in (target / "templates").glob("*horse_racing*"):
        template.unlink()
    assert "protocol_week_source.py" in FILES
    for name in FILES:
        assert (ROOT / name).is_file(), name
    blocks = re.findall(r"<<'(PY|PY_TEMPLATES)'\n(.*?)\n\1\n", SCRIPT, re.S)
    preflights = [body for _, body in blocks if "stage, target" in body]
    assert len(preflights) == 2
    for source in preflights:
        checked = subprocess.run(
            [sys.executable, "-", str(ROOT), str(target), *FILES],
            input=source,
            text=True,
            capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr


def test_collector_restart_selected_only_when_source_changes(tmp_path):
    stage, app = tmp_path / "stage", tmp_path / "app"
    (stage / "code").mkdir(parents=True)
    app.mkdir()
    source = (
        "# The medals worker imports"
        + SCRIPT.split("# The medals worker imports", 1)[1].split("\nbackup=", 1)[0]
    )
    script = (
        "set -euo pipefail\nstage=$1\napp_dir=$2\nservices=(balance-app.service)\n"
        + source
        + '\nprintf "%s\\n" "${services[@]}"\n'
    )
    (stage / "code/protocol_week_source.py").write_text("new collector")
    for installed, expected in (
        ("new collector", ["balance-app.service"]),
        ("old collector", ["balance-app.service", "balance-app-medals.service"]),
    ):
        (app / "protocol_week_source.py").write_text(installed)
        checked = subprocess.run(
            ["bash", "-c", script, "fixture", str(stage), str(app)],
            text=True,
            capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr
        assert checked.stdout.splitlines() == expected
