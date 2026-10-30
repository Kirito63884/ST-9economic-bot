"""Offline pip regression for private installer umask versus website imports."""

import importlib.metadata
import os
from pathlib import Path
import stat
import subprocess
import sys
import venv
import zipfile

import pytest


@pytest.fixture(scope="module")
def chess_wheel(tmp_path_factory):
    """Repack the already-installed public dependency; CI makes no network calls."""
    folder = tmp_path_factory.mktemp("chess-offline-wheel")
    package = importlib.metadata.distribution("chess")
    assert package.version == "1.11.2"
    with zipfile.ZipFile(
        folder / "chess-1.11.2-py3-none-any.whl", "w", zipfile.ZIP_DEFLATED
    ) as wheel:
        for name in package.files:
            if name.parts[0] not in {"chess", "chess-1.11.2.dist-info"} or str(
                name
            ).endswith(".pyc"):
                continue
            path = Path(package.locate_file(name))
            if path.is_file():
                wheel.write(path, str(name))
    return folder


def fixture_environment(tmp_path, chess_wheel):
    app = tmp_path / "app"
    app.mkdir()
    environment = app / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin/python"
    site = Path(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import sysconfig;print(sysconfig.get_paths()['purelib'])",
            ],
            text=True,
        ).strip()
    )
    commands = tmp_path / "bin"
    commands.mkdir()
    # No root/second UID is needed in CI. If the installed package denies access
    # to other users, mask the owner's access for the probe to reproduce EACCES.
    # The child still runs the actual Python import/metadata/Board checks.
    runuser = commands / "runuser"
    runuser.write_text(f"""#!{sys.executable}
import os,stat,subprocess,sys
from pathlib import Path
assert sys.argv[1:4] == ['-u','www-data','--']
package=Path(os.environ['CHESS_TEST_SITE'])/'chess'
original=None
if package.is_dir():
 mode=stat.S_IMODE(package.stat().st_mode)
 init=package/'__init__.py'
 readable=mode & 0o005 == 0o005 and init.is_file() and init.stat().st_mode & 0o004
 if not readable:
  original=mode
  package.chmod(0)
with open(os.environ['CHESS_TEST_PROBES'],'a') as log:log.write('probe\\n')
try:
 result=subprocess.run(sys.argv[4:],input=sys.stdin.buffer.read())
finally:
 if original is not None:package.chmod(original)
sys.exit(result.returncode)
""")
    runuser.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(chess_wheel),
        "PIP_ONLY_BINARY": ":all:",
        "CHESS_TEST_SITE": str(site),
        "CHESS_TEST_PROBES": str(tmp_path / "probes"),
    }
    source = (
        Path(__file__).resolve().parents[1] / "scripts/update_games_and_access.sh"
    ).read_text()
    block = source[
        source.index("install_chess_dependency() (") : source.index("\nchanging=1")
    ]
    runner = tmp_path / "dependency.sh"
    runner.write_text(
        "set -euo pipefail\numask 077\napp_dir=$1\n"
        + block
        + '\numask > "$app_dir/after-umask"\n'
    )
    (app / ".env").write_text("private-fixture-sentinel\n")
    (app / ".env").chmod(0o600)
    return app, python, site, runner, env


@pytest.mark.parametrize("damaged", [False, True])
def test_dependency_is_readable_and_private_umask_survives(
    tmp_path, chess_wheel, damaged
):
    app, python, site, runner, env = fixture_environment(tmp_path, chess_wheel)
    if damaged:
        # This is exactly the old installer path: pip under a private umask.
        result = subprocess.run(
            [
                "bash",
                "-c",
                'umask 077; "$1" -m pip install --disable-pip-version-check --no-deps chess==1.11.2',
                "fixture",
                str(python),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert stat.S_IMODE((site / "chess").stat().st_mode) == 0o700
        assert stat.S_IMODE((site / "chess/__init__.py").stat().st_mode) == 0o600
    result = subprocess.run(
        ["bash", str(runner), str(app)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "probes").read_text().count("probe") == (2 if damaged else 1)
    if damaged:
        assert "недоступный пакет: None" in result.stdout + result.stderr
    for root in (site / "chess", site / "chess-1.11.2.dist-info"):
        for path in (root, *root.rglob("*")):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o004, path
            if path.is_dir():
                assert mode & 0o001, path
    assert (app / "after-umask").read_text().strip() == "0077"
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600
    assert (app / ".env").read_text() == "private-fixture-sentinel\n"


def test_shadow_module_fails_before_source_update(tmp_path, chess_wheel):
    app, python, site, runner, env = fixture_environment(tmp_path, chess_wheel)
    (app / "chess.py").write_text(
        "# Local collision must never pass the dependency preflight.\n"
    )
    result = subprocess.run(
        ["bash", str(runner), str(app)], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "chess.py" in result.stdout + result.stderr
    assert not (app / "after-umask").exists()
    assert (app / ".env").read_text() == "private-fixture-sentinel\n"
