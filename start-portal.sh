#!/bin/sh
# Starts the CN-PK Consignment Portal from the source code (needs Python 3.11 or newer).
# Most people should use the ready-made CNPK-Portal program instead: see docs/05-run-on-your-computer.md
set -e
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install Python 3.11 or newer: https://www.python.org/downloads/"
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "The portal needs Python 3.11 or newer."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First start: installing what the portal needs. This takes a minute and needs the internet."
  "$PYTHON" -m venv .venv || { echo "Could not create .venv (on Ubuntu/Debian: sudo apt install python3-venv)"; exit 1; }
fi
if ! cmp -s launcher/requirements.txt .venv/installed-requirements.txt; then
  .venv/bin/python -m pip install --disable-pip-version-check -q -r launcher/requirements.txt
  cp launcher/requirements.txt .venv/installed-requirements.txt
fi
exec .venv/bin/python launcher/portal_launcher.py "$@"
