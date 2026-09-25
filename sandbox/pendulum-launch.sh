#!/bin/sh
# Launch the Barrett pendulum on the Raspberry Pi.
#   pendulum-launch.sh prompt   boot-time YES/NO prompt (what the login autostart runs)
#   pendulum-launch.sh kiosk    straight into the full-screen pendulum kiosk
#   pendulum-launch.sh gui      engineering GUI (gains, scan, zero, bias)
# Extra options pass through, e.g.
#   pendulum-launch.sh prompt --auto-stop 120   (runs stop after 2 min; 0 = never)
#   pendulum-launch.sh kiosk  --auto-stop 0
# Output goes to ~/.cache/barrett-pendulum/pendulum.log (last run) for SSH debugging.
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY=python3

LOG_DIR="$HOME/.cache/barrett-pendulum"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/pendulum.log"
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"

cd "$HERE" || exit 1
MODE="${1:-prompt}"
[ $# -gt 0 ] && shift
case "$MODE" in
    prompt)
        # Start as soon as the desktop is up (no delay): the prompt doesn't
        # need CAN, and the kiosk connects/retries on its own.
        # PENDULUM_START_DELAY=<s> adds a delay if ever needed.
        sleep "${PENDULUM_START_DELAY:-0}"
        exec "$PY" boot_prompt.py "$@" >>"$LOG" 2>&1 ;;
    kiosk) exec "$PY" furuta_pendulum.py --touchscreen "$@" >>"$LOG" 2>&1 ;;
    gui)   exec "$PY" furuta_pendulum.py >>"$LOG" 2>&1 ;;
    *) echo "usage: $0 [prompt|kiosk|gui]" >&2; exit 2 ;;
esac
