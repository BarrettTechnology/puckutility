#!/bin/sh
# Launch the Barrett pendulum on the Raspberry Pi.
#   pendulum-launch.sh prompt   boot-time YES/NO prompt (what the login autostart runs)
#   pendulum-launch.sh kiosk    straight into the full-screen pendulum kiosk
#   pendulum-launch.sh gui      engineering GUI (gains, scan, zero, bias)
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
        # Autostart fires as the desktop comes up; give the compositor and
        # the CAN bring-up (can-up@can0.service) a moment first.
        sleep "${PENDULUM_START_DELAY:-3}"
        exec "$PY" boot_prompt.py "$@" >>"$LOG" 2>&1 ;;
    kiosk) exec "$PY" furuta_pendulum.py --touchscreen >>"$LOG" 2>&1 ;;
    gui)   exec "$PY" furuta_pendulum.py >>"$LOG" 2>&1 ;;
    *) echo "usage: $0 [prompt|kiosk|gui]" >&2; exit 2 ;;
esac
