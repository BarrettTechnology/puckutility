#!/bin/bash
# One command to bring the pendulum Pi up to date and ready for a demo:
#   - pulls the latest code
#   - turns GNOME's on-screen keyboard on (appears when a text field is tapped)
#   - restarts the pendulum kiosk with the new code
#
#   ~/puckutility/sandbox/update-pi.sh
#
# Run it from a Terminal on the Pi's own desktop.
set -u
cd "$(dirname "$0")/.." || exit 1

echo "==> Updating code"
git pull --ff-only || echo "!!  git pull failed (no internet?) -- continuing with the current code"
git log -1 --oneline

echo "==> On-screen keyboard on"
gsettings set org.gnome.desktop.a11y.applications screen-keyboard-enabled true \
    && echo "OK  GNOME keyboard appears when you tap a text field"

echo "==> Restarting the pendulum kiosk"
pkill -f 'furuta_pendulum.py|furuta_kiosk.py|boot_prompt.py' 2>/dev/null && sleep 2
setsid nohup "$PWD/sandbox/pendulum-launch.sh" kiosk >/dev/null 2>&1 &
echo "OK  kiosk starting (a few seconds)"
