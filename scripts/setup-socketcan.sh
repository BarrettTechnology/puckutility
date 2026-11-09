#!/bin/sh
# Set up SocketCAN auto-configuration for Peak PCAN / CANable / generic adapters.
#
# Portable across Ubuntu 20.04 -> 26.04. Interfaces are brought up by a
# udev-triggered systemd service (the same mechanism the .deb installer uses),
# NOT by an /etc/network/interfaces (ifupdown) stanza. Newer Ubuntu (netplan +
# NetworkManager / systemd-networkd) does not run ifupdown, so the old stanza was
# silently ignored on 22.04+ and 26.04 -- which is why can0 never came up.
#
# Re-runnable (idempotent): everything is copied/overwritten in place.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Use sudo only when not already root and sudo exists.
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO=sudo; fi

# --- reset helper -------------------------------------------------------------
$SUDO cp reset_can.sh /usr/bin/
$SUDO chmod 755 /usr/bin/reset_can.sh

# --- udev rules + systemd bring-up service -----------------------------------
# 60-can.rules     : device permissions for Peak PCAN (open without sudo)
# 90-canable.rules : device permissions + bring-up for CandleLight/CANable + DFU
# 61-can-up.rules  : when any can* netdev appears, start can-up@<iface>.service
# can-up@.service  : runs the `ip link set ... up` commands (bit timing) in a full
#                    environment (udev's RUN+= is too restricted on 22.04+)
# Installed under /usr/lib, where the puck-can-* packages put them.  An /etc
# file of the same name OVERRIDES the /usr/lib one (an older app's, or this
# script's before it moved): those are moved aside to <name>.puck-old, or
# can0 comes up at their timing and goes bus-off under load.
if dpkg-query -W -f='${Status}' puck-can-common 2>/dev/null | grep -q 'ok installed'; then
    echo "puck-can-common is installed: its rules and service are used as they are."
else
    $SUDO install -Dm644 60-can.rules     /usr/lib/udev/rules.d/60-can.rules
    $SUDO install -Dm644 61-can-up.rules  /usr/lib/udev/rules.d/61-can-up.rules
    $SUDO install -Dm644 90-canable.rules /usr/lib/udev/rules.d/90-canable.rules
    $SUDO install -Dm644 can-up@.service  /usr/lib/systemd/system/can-up@.service
fi
for f in /etc/udev/rules.d/60-can.rules /etc/udev/rules.d/61-can-up.rules \
         /etc/udev/rules.d/90-canable.rules /etc/systemd/system/can-up@.service; do
    if [ -e "$f" ]; then
        echo "Moving $f aside to $f.puck-old (it overrides the /usr/lib copy)"
        $SUDO mv -f "$f" "$f.puck-old"
    fi
done

# --- NetworkManager: leave SocketCAN alone ------------------------------------
# Stops NM from racing the service or tearing can0 back down. No-op if NM absent.
# App-neutral filename so puckutility, pucktuner and the .deb all write the SAME
# file -- whichever configured CAN first, the others need not run again.
$SUDO mkdir -p /etc/NetworkManager/conf.d
printf '[keyfile]\nunmanaged-devices=type:can\n' \
    | $SUDO tee /etc/NetworkManager/conf.d/99-puck-can.conf >/dev/null

# --- apply now (and to any already-attached adapter) --------------------------
$SUDO udevadm control --reload-rules
$SUDO udevadm trigger
if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl daemon-reload
    # Re-run the bring-up on interfaces already up, at the installed timing.
    for i in $(ls /sys/class/net 2>/dev/null | grep '^can[0-9]'); do
        $SUDO systemctl restart "can-up@$i.service" 2>/dev/null || true
    done
    if systemctl is-active --quiet NetworkManager 2>/dev/null; then
        $SUDO systemctl reload NetworkManager 2>/dev/null \
            || $SUDO systemctl restart NetworkManager 2>/dev/null || true
    fi
fi

$SUDO apt install -y can-utils

echo
echo "SocketCAN setup complete."
echo "Plug in (or replug) the CAN adapter; can0 comes up automatically (CAN-FD 1M/5M,"
echo "sample point 68.7 %; check with: ip -details link show can0)."
echo "Manual reset any time:  reset_can.sh"
