#!/usr/bin/env bash
# Rebuild the guide PDFs from docs/*/guide.md (p4core/guide/README.md has the format).
#
#   scripts/build-guide.sh               new screenshots from the real app, then the PDF (the default).
#                                        Falls back to the committed screenshots, with a warning, when
#                                        the simulator / vcan0 / the app's venv is not available.
#   scripts/build-guide.sh --shots       the same, but screenshots are required: fail instead of falling back
#   scripts/build-guide.sh --no-shots    text only: the PDF from the committed screenshots
#
# Screenshots: the app runs against the puck simulator on vcan0. The simulator is $P4GUIDE_SIM, else
# ~/stm32/sim/puck_sim; the app's Python is $PYTHON, else .venv/bin/python. Needs (checked below):
# Python 3, Chrome / Chromium / Edge, aspell + aspell-en (spelling; skipped with a note if missing).
set -e
cd "$(dirname "$0")/.."
GUIDES=$(ls docs/*/guide.md)
MODE=auto
case "${1:-}" in
    --shots) MODE=require ;;
    --no-shots) MODE=off ;;
    "") ;;
    *) echo "usage: $0 [--shots | --no-shots]"; exit 2 ;;
esac
PY=${PYTHON:-.venv/bin/python}
[ -x "$PY" ] || PY=python3
SIM=${P4GUIDE_SIM:-$HOME/stm32/sim/puck_sim}

missing=0
need() { echo "missing: $1"; echo "   fix: $2"; missing=1; }
command -v "$PY" >/dev/null 2>&1 || need "Python 3 ($PY)" "install python3, or set PYTHON=/path/to/python"
"$PY" -c "import sys; sys.path.insert(0, '.'); from p4core.guide import build; build.find_chrome()" >/dev/null 2>&1 ||
    need "Chrome / Chromium / Edge" "sudo apt install chromium-browser (or google-chrome), or set P4GUIDE_CHROME=/path/to/chrome"
if ! command -v aspell >/dev/null 2>&1 || ! aspell dicts 2>/dev/null | grep -q '^en_US'; then
    echo "note: aspell + aspell-en not found: spelling will not be checked (sudo apt install aspell aspell-en)"
fi
[ "$missing" = 0 ] || exit 1

# --- can the screenshots be taken here? -------------------------------------------------------------
why=""
if [ "$MODE" != off ]; then
    if [ "$(uname -s)" != Linux ]; then why="screenshots need Linux"
    elif [ ! -x "$SIM" ]; then why="no puck simulator at $SIM (set P4GUIDE_SIM, or build it: make -f Makefile.host in the firmware repo's sim/)"
    elif ! "$PY" -c "import wx, canopen" >/dev/null 2>&1; then why="$PY has no wx / canopen (set PYTHON to the app's venv Python)"
    elif ! ip link show vcan0 >/dev/null 2>&1; then
        setup="$(dirname "$SIM")/setup_vcan.sh"
        if [ -t 0 ] && [ -f "$setup" ]; then
            read -r -p "vcan0 (the simulator's virtual CAN bus) is down. Bring it up now with sudo? [y/N] " ans
            [ "$ans" = y ] || [ "$ans" = Y ] && sudo bash "$setup" >/dev/null
        fi
        ip link show vcan0 >/dev/null 2>&1 || why="vcan0 is down (sudo bash $setup)"
    fi
fi

if [ "$MODE" != off ] && [ -z "$why" ]; then
    for GUIDE in $GUIDES; do
        echo "$GUIDE: taking new screenshots (the app on the simulator, about 1-2 min)..."
        P4GUIDE_APP_PYTHON="$PY" "$PY" -m p4core.guide all "$GUIDE" --sim "$SIM"
    done
    exit 0
fi
if [ "$MODE" = require ]; then
    echo "screenshots required (--shots) but: $why"; exit 1
fi
[ -n "$why" ] && echo "WARNING: screenshots NOT retaken: $why. Using the committed ones."
for GUIDE in $GUIDES; do
    if note=$("$PY" -m p4core.guide stale "$GUIDE"); then :; else
        echo "WARNING: $GUIDE: $note: they may be out of date. Retake them before a release."
    fi
    "$PY" -m p4core.guide build "$GUIDE"
done
