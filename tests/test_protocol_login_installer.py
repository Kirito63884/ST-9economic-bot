"""Installer patches retain installed games and refuse unknown shared layouts."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_patch_shared_files_is_idempotent_and_preserves_other_features(tmp_path):
    script = (ROOT / "scripts/update_protocol_web_login.sh").read_text()
    patch = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    app, stage = tmp_path / "app", tmp_path / "stage"
    for path in (app, stage):
        (path / "templates").mkdir(parents=True)
    original_main = (
        ROOT / "main.py"
    ).read_text() + '\nEXTRA_INSTALLED_GAME = "preserve-this"\n'
    original_html = (
        ROOT / "templates/_admin_audit.html"
    ).read_text() + "\n<!-- existing game tabs -->\n"
    old_main = (
        original_main.replace(
            '        or request.url.path == "/api/admin/protocol-login"\n', ""
        )
        .replace(
            "from protocol_login_admin import make_router as make_protocol_login_router\n",
            "",
        )
        .replace("app.include_router(make_protocol_login_router(is_super_admin))\n", "")
    )
    (app / "main.py").write_text(old_main)
    (app / "templates/_admin_audit.html").write_text(
        original_html.replace(
            '    {% if is_super %}{% include "_protocol_login.html" %}{% endif %}\n', ""
        )
    )
    (stage / "templates/_protocol_login.html").write_text(
        (ROOT / "templates/_protocol_login.html").read_text()
    )
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-", str(app), str(stage)],
            input=patch,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert (
            'EXTRA_INSTALLED_GAME = "preserve-this"' in (stage / "main.py").read_text()
        )
        assert (
            "<!-- existing game tabs -->"
            in (stage / "templates/_admin_audit.html").read_text()
        )
        for name in ("main.py", "templates/_admin_audit.html"):
            (app / name).write_text((stage / name).read_text())
    assert (app / "main.py").read_text() == original_main
    assert (app / "templates/_admin_audit.html").read_text() == original_html
    (app / "main.py").write_text("UNKNOWN_LAYOUT = True")
    result = subprocess.run(
        [sys.executable, "-", str(app), str(stage)],
        input=patch,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert (app / "main.py").read_text() == "UNKNOWN_LAYOUT = True"
