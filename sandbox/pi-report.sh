#!/bin/bash
# Collect EVERYTHING needed to debug the pendulum Pi into one text file.
# Read-only: changes nothing. Asks for your password once (kernel log,
# bootloader info).
#
#   ./pi-report.sh            -> ~/pi-report-<date>-<time>.txt
#
# Then email/copy that file to wherever it's needed.
# No passwords, Wi-Fi keys or personal files are included.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HOME/pi-report-$(date +%Y%m%d-%H%M).txt"
sudo -v 2>/dev/null || echo "(no sudo -- some sections will be partial)"

sec() { printf '\n\n########## %s ##########\n' "$*"; }
run() { printf '\n$ %s\n' "$*"; timeout 30 bash -c "$*" 2>&1; }

{
sec "SYSTEM"
run 'date; uptime'
run '. /etc/os-release; echo "$PRETTY_NAME"; uname -a'
run "tr -d '\0' </proc/device-tree/model; echo"
run 'echo "session=$XDG_SESSION_TYPE desktop=$XDG_CURRENT_DESKTOP display=$DISPLAY wayland=$WAYLAND_DISPLAY"'
run 'cd "'"$HERE"'/.." && git log -1 --oneline && git status --short | head'

sec "DISPLAY: boot config"
run 'cat /boot/firmware/config.txt'
run 'cat /boot/firmware/cmdline.txt'
run 'ls -la /boot/firmware/*.bak* 2>/dev/null'

sec "DISPLAY: connectors now"
run 'for c in /sys/class/drm/card*-*; do echo "$(basename $c): status=$(cat $c/status 2>/dev/null) enabled=$(cat $c/enabled 2>/dev/null) modes=$(tr "\n" " " <$c/modes 2>/dev/null)"; done'
run 'ls /sys/class/backlight/ 2>/dev/null; for b in /sys/class/backlight/*; do echo "$b: brightness=$(cat $b/brightness) max=$(cat $b/max_brightness) bl_power=$(cat $b/bl_power 2>/dev/null)"; done'
run 'cat ~/.config/monitors.xml 2>/dev/null'
run 'ls -la /var/lib/gdm3/.config/monitors.xml 2>/dev/null && sudo cat /var/lib/gdm3/.config/monitors.xml'

sec "DISPLAY: kernel messages (this boot)"
run "sudo journalctl -k -b 0 --no-pager | grep -i -E 'drm|dsi|panel|ili9881|hdmi|vc4|backlight|goodix|touch|fb0|display' | tail -60"

sec "BOOTS"
run 'journalctl --list-boots --no-pager | tail -8'
for b in -1 -2 -3; do
    sec "BOOT $b: display kernel messages"
    run "sudo journalctl -k -b $b --no-pager | grep -i -E 'drm|dsi|panel|ili9881|hdmi|vc4|backlight|goodix|touch' | tail -40"
    sec "BOOT $b: errors"
    run "sudo journalctl -b $b -p err --no-pager | tail -40"
    sec "BOOT $b: how it ended (last 40 lines)"
    run "sudo journalctl -b $b -n 40 --no-pager -o short-monotonic"
done

sec "THIS BOOT: errors + GNOME/GDM"
run "sudo journalctl -b 0 -p err --no-pager | tail -60"
run "journalctl -b 0 --no-pager | grep -i -E 'gnome-shell|gdm|mutter' | grep -i -E 'error|warn|fail|monitor|output|extension' | tail -60"

sec "REBOOT CHECK"
run '"'"$HERE"'/reboot-check.sh" </dev/null'

sec "BOOT SPEED"
run '"'"$HERE"'/boot-speed.sh"'

sec "ON-SCREEN KEYBOARD"
run 'gnome-shell --version'
run 'gnome-extensions list --enabled; echo "-- all:"; gnome-extensions list'
run 'gnome-extensions info no-touch-osk@barrett.com'
run 'ls -la ~/.local/share/gnome-shell/extensions/no-touch-osk@barrett.com/'
run 'gsettings get org.gnome.shell enabled-extensions; gsettings get org.gnome.shell disable-user-extensions'
run 'gsettings get org.gnome.desktop.a11y.applications screen-keyboard-enabled'
run "journalctl --user -b 0 --no-pager | grep -i -E 'no-touch-osk|extension' | tail -30"
run '"'"$HERE"'/disable-onscreen-keyboard.sh" --check'

sec "KIOSK"
run 'ls -la ~/.config/autostart/'
run 'cat ~/.config/autostart/barrett-pendulum.desktop'
run 'tail -60 ~/.cache/barrett-pendulum/pendulum.log'
run 'tail -20 ~/.cache/barrett-pendulum/pendulum.log.1'
run 'ls -la "'"$HERE"'/.venv/bin/python" && "'"$HERE"'/.venv/bin/python" -c "import wx, canopen; print(wx.version(), canopen.__version__)"'
run 'sudo grep -v "^#" /etc/gdm3/custom.conf | grep -v "^$"'
run 'gsettings get org.gnome.desktop.session idle-delay; gsettings get org.gnome.desktop.screensaver lock-enabled'

sec "CAN"
run 'lsusb'
run 'ip -details link show | grep -A6 -E "^[0-9]+: can"'
run 'systemctl status "can-up@*" --no-pager | head -20'

sec "NETWORK (no secrets)"
run 'nmcli -f DEVICE,TYPE,STATE,CONNECTION device status'
run 'ip -br addr'

sec "SETUP-PI CHECK"
run '"'"$HERE"'/setup-pi.sh" --check'
} > "$OUT" 2>&1

# strip terminal colour codes so the file reads cleanly
sed -i 's/\x1b\[[0-9;]*m//g' "$OUT"
echo "Wrote $OUT ($(wc -l <"$OUT") lines). Email/copy that file over."
