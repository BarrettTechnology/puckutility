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

# --- udev rules ---------------------------------------------------------------
# 60-can.rules     : device permissions for Peak PCAN (open without sudo)
# 90-canable.rules : device permissions + bring-up for CandleLight/CANable + DFU
# 61-can-up.rules  : when any can* netdev appears, start can-up@<iface>.service
$SUDO cp 60-can.rules 61-can-up.rules 90-canable.rules /etc/udev/rules.d/

# --- systemd bring-up service -------------------------------------------------
# Started by 61-can-up.rules; runs the `ip link set ... up` commands in a full
# environment (udev's RUN+= is too restricted on 22.04+).
$SUDO cp can-up@.service /etc/systemd/system/

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
    if systemctl is-active --quiet NetworkManager 2>/dev/null; then
        $SUDO systemctl reload NetworkManager 2>/dev/null \
            || $SUDO systemctl restart NetworkManager 2>/dev/null || true
    fi
fi

$SUDO apt install -y can-utils

echo
echo "SocketCAN setup complete."
echo "Plug in (or replug) the CAN adapter; can0 comes up automatically at 1 Mbit/s."
echo "Manual reset any time:  reset_can.sh"
