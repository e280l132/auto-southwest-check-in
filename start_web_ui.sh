#!/usr/bin/env bash
# Starts the Auto Southwest Check-In web UI.
# Usage: ./start_web_ui.sh [port]

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PORT="${1:-9000}"
VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install -q -r requirements.txt

exec python3 southwest.py --web --web-port "$PORT"
