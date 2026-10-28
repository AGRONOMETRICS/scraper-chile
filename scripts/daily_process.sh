#!/usr/bin/env bash
# Daily run: the whole DecoFrut report (every season it currently serves).

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# Activate the virtualenv: env/bin on Linux/macOS, env/Scripts on Windows.
if [ -f env/bin/activate ]; then
    . env/bin/activate
elif [ -f env/Scripts/activate ]; then
    . env/Scripts/activate
else
    echo "No virtualenv in env/ (run: python -m venv env)" >&2
    exit 1
fi

echo "venv: $VIRTUAL_ENV"

echo "===== main.py (whole report) ====="
python main.py "$@"
