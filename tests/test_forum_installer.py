"""Run the installer's actual Python probe against the isolated test app."""

from pathlib import Path

import pytest
import main


def probe():
    script = (
        Path(__file__).resolve().parents[1] / "scripts/update_forum_events.sh"
    ).read_text()
    return script.split("runuser -u www-data -- ./venv/bin/python - <<'PY'\n", 1)[
        1
    ].split("\nPY\n", 1)[0]


def test_installer_probe_with_real_nested_routers():
    # Includes the router objects which have no .path in FastAPI 0.142.
    exec(compile(probe(), "forum-installer-probe", "exec"), {})


def test_installer_probe_rejects_missing_route(monkeypatch):
    schema = main.app.openapi()
    paths = dict(schema["paths"])
    paths.pop("/api/admin/forum-events")
    monkeypatch.setattr(main.app, "openapi", lambda: {**schema, "paths": paths})
    with pytest.raises(AssertionError):
        exec(compile(probe(), "forum-installer-probe", "exec"), {})
