#!/bin/bash
# Display guard for the pendulum Pi (replaces the older touch guard).
#
#   sudo sandbox/install-display-guard.sh           install / update
#   sudo sandbox/install-display-guard.sh --undo    remove
#
# At every boot, before the desktop starts, it:
#   1. logs the power status (vcgencmd get_throttled + the 5 V rail), so a
#      bad boot can be matched against under-voltage;
#   2. checks the Touch Display came up (a DSI connector reporting "connected").
#      Sometimes the firmware misses the panel at power-on and it doesn't exist
#      for Linux at all -- the screen stays black. Then the guard REBOOTS, at
#      most 3 times in a row (the count resets on a good boot), so a missed
#      panel heals itself instead of waiting for someone to power-cycle.
# No boot settings are touched. Disable without uninstalling:
#   sudo touch /etc/pendulum-display-guard.disable
# See what it did:   journalctl -b -u pendulum-display-guard
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
BIN=/usr/local/sbin/pendulum-display-guard
UNIT=/etc/systemd/system/pendulum-display-guard.service

if [ "${1:-}" = "--undo" ]; then
    systemctl disable pendulum-display-guard.service 2>/dev/null
    rm -f "$UNIT" "$BIN"
    systemctl daemon-reload
    echo "OK  display guard removed"
    exit 0
fi

cat > "$BIN" <<'GUARD'
#!/bin/sh
# Barrett pendulum display guard -- installed by sandbox/install-display-guard.sh
STATE=/var/lib/pendulum-display-guard
MAX_TRIES=3
mkdir -p "$STATE"

# 1. power status
thr=$(vcgencmd get_throttled 2>/dev/null)
v5=$(vcgencmd pmic_read_adc EXT5V_V 2>/dev/null | sed 's/.*=//')
echo "power: ${thr:-throttled=?} 5V=${v5:-?}"

[ -e /etc/pendulum-display-guard.disable ] && { echo "disabled by /etc/pendulum-display-guard.disable"; exit 0; }

# 2. touch display present? (give the drivers up to ~10 s)
dsi_ok() { for c in /sys/class/drm/card*-DSI-*; do [ "$(cat "$c/status" 2>/dev/null)" = connected ] && return 0; done; return 1; }
for i in 1 2 3 4 5 6 7 8 9 10; do
    if dsi_ok; then
        touch_state=$(grep -qi goodix /proc/bus/input/devices && echo "touch OK" || echo "touch MISSING")
        echo "display OK ($touch_state) after ${i}s"
        echo 0 > "$STATE/fails"
        exit 0
    fi
    sleep 1
done

fails=$(( $(cat "$STATE/fails" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE/fails"
echo "display MISSING (no DSI connector) -- consecutive misses: $fails"
if [ "$fails" -le "$MAX_TRIES" ]; then
    echo "rebooting to retry the panel ($fails/$MAX_TRIES)"
    sync
    systemctl reboot
else
    echo "giving up after $MAX_TRIES tries -- power-cycle the Pi"
fi
exit 1
GUARD
chmod 755 "$BIN"

cat > "$UNIT" <<'EOF'
[Unit]
Description=Barrett pendulum: log power, reboot if the touch display was missed
After=systemd-udevd.service systemd-modules-load.service
Before=display-manager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pendulum-display-guard
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# retire the older touch guard (it only re-probed touch / reverted config)
if [ -e /etc/systemd/system/pendulum-touch-guard.service ]; then
    systemctl disable pendulum-touch-guard.service 2>/dev/null
    rm -f /etc/systemd/system/pendulum-touch-guard.service /usr/local/sbin/pendulum-touch-guard
    echo "OK  old touch guard removed"
fi
systemctl daemon-reload
systemctl enable pendulum-display-guard.service >/dev/null 2>&1 && echo "OK  display guard installed + enabled"
echo "    journalctl -b -u pendulum-display-guard   shows what it did each boot"
