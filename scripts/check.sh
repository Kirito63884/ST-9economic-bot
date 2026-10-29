#!/usr/bin/env bash
# Same entry point locally and in GitHub Actions; no production services start.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit('Use Python 3.12, as tested in this repository.')
print('Python:', sys.version.split()[0])
PY
if ! command -v node >/dev/null; then
    echo 'Node.js is required for rendered JavaScript checks (Node 22 recommended).'
    exit 1
fi
node --version
python -m ruff check . --select E9,F
for script in scripts/*.sh; do bash -n "$script"; done
# Do not load a developer's .env when imports run in tests and child processes.
export PYTHON_DOTENV_DISABLED=1
python -m pytest -q tests "$@"
