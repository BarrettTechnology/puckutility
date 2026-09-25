#!/bin/bash
# Pi side: a permanent maintenance link over the Ethernet cable, so the Pi can
# always be reached from a laptop plugged straight into it -- even when the
# touch screen is dark.
#
#   puckutility/sandbox/setup-lan-link.sh      (then your password)
#
#   Pi eth0      192.168.100.66/24   fixed, no DHCP/router needed, no gateway
#                                    (internet keeps going over Wi-Fi)
#   laptop       192.168.100.100/24  (sandbox/pi-connect.sh sets that side up)
#   SSH server   installed + enabled
#
# Blind-friendly: Caps Lock light blinks 5x when done, 2x fast on failure.
# Undo: sudo nmcli connection delete pendulum-link
set -u
PI_ADDR=192.168.100.66/24
CON=pendulum-link
blink() { for i in $(seq "$1"); do setleds +caps 2>/dev/null; sleep "$2"; setleds -caps 2>/dev/null; sleep "$2"; done; }
fail() { echo "XX  $*"; blink 2 0.15; exit 1; }

sudo -v || fail "sudo failed"

IFACE=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null | awk -F: '$2=="ethernet"{print $1; exit}')
IFACE=${IFACE:-eth0}

# 1. fixed-address profile on the Ethernet port (wins over the DHCP one)
if nmcli -t -f NAME connection show | grep -qx "$CON"; then
    sudo nmcli connection modify "$CON" connection.interface-name "$IFACE" \
        ipv4.method manual ipv4.addresses "$PI_ADDR" ipv4.never-default yes \
        ipv6.method link-local connection.autoconnect yes \
        connection.autoconnect-priority 100 || fail "couldn't update $CON"
else
    sudo nmcli connection add type ethernet ifname "$IFACE" con-name "$CON" \
        ipv4.method manual ipv4.addresses "$PI_ADDR" ipv4.never-default yes \
        ipv6.method link-local connection.autoconnect yes \
        connection.autoconnect-priority 100 || fail "couldn't create $CON"
fi
sudo nmcli connection up "$CON" >/dev/null 2>&1 \
    || echo "..  $CON will come up when the cable is connected"
echo "OK  $IFACE -> ${PI_ADDR%/*} (profile '$CON', survives reboots)"

# 2. SSH server
if ! dpkg -s openssh-server >/dev/null 2>&1; then
    sudo apt-get install -y openssh-server >/dev/null || fail "couldn't install openssh-server"
fi
sudo systemctl enable --now ssh >/dev/null 2>&1 || sudo systemctl enable --now ssh.socket >/dev/null 2>&1
systemctl is-active --quiet ssh || systemctl is-active --quiet ssh.socket \
    || fail "SSH server isn't running"
echo "OK  SSH server running"

echo "Done. From the laptop (cable plugged in):  sandbox/pi-connect.sh"
blink 5 0.3
