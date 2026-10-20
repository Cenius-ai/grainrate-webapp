#!/usr/bin/env bash
# GrainRate install: project-level dependencies only, then exit.
# Never installs system packages and never starts anything long-running.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found. Install Python 3.9+ first (see INSTALL.md)." >&2
  exit 1
fi

# Old pip silently fails editable installs on modern pyproject layouts.
"$PYTHON" -m pip install --upgrade pip setuptools wheel

# Test harness (pytest). No runtime dependencies exist.
"$PYTHON" -m pip install -r requirements.txt

# Console entry point: puts `grainrate` on PATH. --no-build-isolation keeps the
# install offline-friendly by reusing the setuptools/wheel just upgraded above.
if ! "$PYTHON" -m pip install --no-build-isolation -e . ; then
  echo "error: could not install the grainrate console script; retrying with build isolation" >&2
  "$PYTHON" -m pip install -e .
fi

# Fails loudly here if anything needed by the CLI is missing, instead of at boot.
"$PYTHON" -c "import grainrate; assert grainrate.SAFE_STORAGE_TABLE"

echo "Setup complete."
echo "Run:   $PYTHON grainrate.py --help      (or: grainrate --help)"
echo "Demo:  bash demo.sh"
echo "Test:  $PYTHON -m pytest"
