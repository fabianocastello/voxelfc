#!/usr/bin/env bash
# VOXEL FC installer for Linux (thor25, leno18) and macOS (MacBook Air M1).
# Each machine runs fully independently: its own venv, its own models in
# ~/.voxelfc/models. Nothing is shared over the network.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

OS="$(uname -s)"

echo "== VOXEL FC: installation (${OS}) =="

# --- 1. Check Python 3.10+ ---
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python3 not found in PATH. Install Python 3.10+ before continuing." >&2
    exit 1
fi

# --- 2. Check FFmpeg ---
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "FFmpeg not found."
    if [ "$OS" = "Darwin" ]; then
        echo "Install with: brew install ffmpeg"
    else
        echo "Install with: sudo apt install ffmpeg   (or your distro's package manager)"
    fi
    exit 1
fi

# --- 3. Create venv ---
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv ..."
    "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip -q

# --- 4. Detect extras (GPU/mlx) - shared with update.sh so both always agree ---
# shellcheck disable=SC1091
source "$REPO_DIR/scripts/_detect_extras.sh"

echo "Installing the package (extras: ${EXTRAS}) ..."
python -m pip install -q -e ".[${EXTRAS}]"

# --- 5. Create .env from the example, if needed ---
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example. Fill in DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, and HF_TOKEN."
fi

echo ""
echo "Installation complete on this machine."
echo "Models and cache will live in: ~/.voxelfc/models"
echo "To use it:"
echo "  source .venv/bin/activate"
echo "  voxelfc --source /path/to/audio.mp3"
