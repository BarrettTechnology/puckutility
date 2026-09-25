#!/bin/bash
# Laptop side: reach the pendulum Pi over a direct Ethernet cable.
#
#   sandbox/pi-connect.sh              set up this laptop's port, check, open a shell
#   sandbox/pi-connect.sh <command>    run one command on the Pi instead
#
# Needs sandbox/setup-lan-link.sh run once on the Pi (gives it 192.168.100.66).
# This laptop's wired port gets 192.168.100.100/24 via a NetworkManager profile
# ('pendulum-link-laptop', no gateway -- Wi-Fi keeps the internet). The first
# time, it installs your SSH key on the Pi (asks for the Pi's password once).
set -u
PI=192.168.100.66
ME=192.168.100.100/24
USER_ON_PI=${PI_USER:-robot}
CON=pendulum-link-laptop

IFACE=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null | awk -F: '$2=="ethernet"{print $1; exit}')
[ -n "$IFACE" ] || { echo "No wired Ethernet port found on this laptop."; exit 1; }

# 1. laptop side of the link (only if the port doesn't already have an address in that subnet)
if ! ip -4 addr show "$IFACE" | grep -q '192\.168\.100\.'; then
    if ! nmcli -t -f NAME connection show | grep -qx "$CON"; then
        nmcli connection add type ethernet ifname "$IFACE" con-name "$CON" \
            ipv4.method manual ipv4.addresses "$ME" ipv4.never-default yes \
            ipv6.method link-local connection.autoconnect no >/dev/null \
            || { echo "Couldn't create $CON (try with sudo)"; exit 1; }
    fi
    nmcli connection up "$CON" ifname "$IFACE" >/dev/null || { echo "Couldn't bring up $CON"; exit 1; }
    sleep 1
fi
echo "Laptop: $IFACE $(ip -4 -br addr show "$IFACE" | awk '{print $3}')"

# 2. is the Pi there?
if [ "$(cat /sys/class/net/$IFACE/carrier 2>/dev/null)" != 1 ]; then
    echo "No link on $IFACE -- is the cable plugged into both the laptop and the Pi?"; exit 1
fi
for i in 1 2 3 4 5; do ping -c 1 -W 1 $PI >/dev/null 2>&1 && break; sleep 1; done
if ! ping -c 1 -W 1 $PI >/dev/null 2>&1; then
    echo "The Pi isn't answering at $PI."
    echo "  - Has sandbox/setup-lan-link.sh been run on the Pi? (once, ever)"
    echo "  - Is the Pi powered and finished booting (give it ~1 min)?"
    exit 1
fi
echo "Pi:     $PI answers"

# 3. key-based login (password once, the first time)
if ! ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new \
        "$USER_ON_PI@$PI" true 2>/dev/null; then
    echo "Installing your SSH key on the Pi -- enter the Pi's password for '$USER_ON_PI':"
    ssh-copy-id -o StrictHostKeyChecking=accept-new "$USER_ON_PI@$PI" || exit 1
fi

# 4. shell or one command
if [ $# -gt 0 ]; then
    exec ssh "$USER_ON_PI@$PI" "$@"
else
    echo "Connected. (exit to leave)"
    exec ssh "$USER_ON_PI@$PI"
fi
