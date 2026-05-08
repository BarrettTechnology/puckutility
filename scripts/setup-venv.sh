#!/bin/sh
sudo apt update
sudo apt install -y python3-dev python3-venv python3-wheel libgtk-3-dev

# Set up the virtual environment in the project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 -m venv "$SCRIPT_DIR/.."

# Activate with: source scripts/activate
