#!/bin/bash
# Why does the Pi take so long to boot? -- and fix the usual culprits.
#
#   ./boot-speed.sh         report only (changes nothing); also saved to
#                           ~/.cache/barrett-pendulum/boot-speed.txt for pasting
#   ./boot-speed.sh --fix   disable the common slow-boot services a kiosk doesn't need
#
# A boot that sits ~2 minutes on the logo is almost always a service waiting for
# something that never comes, until its 90-120 s timeout -- typically "wait for
# the network to be online" with no Ethernet cable plugged in.
#
# --fix only touches these (each only if it's present and enabled):
#   systemd-networkd-wait-online / NetworkManager-wait-online
#       "wait until the network is up" -- the kiosk starts fine without it;
#       networking itself keeps working exactly as before
#   cloud-init        first-boot provisioning on Ubuntu images; not needed afterwards
#   ModemManager      cellular-modem support (also probes USB serial devices)
set -u
FIX=0; [ "${1:-}" = "--fix" ] && FIX=1
OUT_DIR="$HOME/.cache/barrett-pendulum"; mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/boot-speed.txt"

report() {
    . /etc/os-release 2>/dev/null
    echo "== $(date '+%F %T')  ${PRETTY_NAME:-?}  $(uname -r)  $(tr -d '\0' 2>/dev/null </proc/device-tree/model)"
    echo
    echo "== Boot time (firmware / kernel / services)"
    systemd-analyze 2>&1
    echo
    echo "== Slowest services"
    systemd-analyze blame 2>/dev/null | head -15
    echo
    echo "== What the desktop waited on (critical chain)"
    target=graphical.target
    systemd-analyze critical-chain "$target" 2>/dev/null | head -25
    echo
    echo "== Usual culprits"
    for u in systemd-networkd-wait-online.service NetworkManager-wait-online.service \
             cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service \
             ModemManager.service snapd.seeded.service plymouth-quit-wait.service; do
        state=$(systemctl is-enabled "$u" 2>/dev/null) || state=${state:-absent}
        t=$(systemd-analyze blame 2>/dev/null | awk -v u="$u" '$NF==u{print $1}')
        printf '   %-38s %-10s %s\n' "$u" "$state" "${t:+took $t}"
    done
    [ -e /etc/cloud/cloud-init.disabled ] && echo "   (cloud-init disabled via /etc/cloud/cloud-init.disabled)"
    echo
    echo "== Boot order (Pi 5 EEPROM)"
    if command -v rpi-eeprom-config >/dev/null 2>&1; then
        rpi-eeprom-config 2>/dev/null | grep -E 'BOOT_ORDER|PCIE_PROBE' \
            || echo "   (no BOOT_ORDER set -- default tries SD first)"
        echo "   BOOT_ORDER is read right-to-left: 0xf416 = NVMe(6), then SD(1), then USB(4), retry(f)"
    else
        echo "   rpi-eeprom-config not installed (sudo apt install rpi-eeprom to check)"
    fi
}

if [ $FIX = 0 ]; then
    report 2>&1 | tee "$OUT"
    echo
    echo "Saved to $OUT -- paste that file, or run  $0 --fix"
    exit 0
fi

# ── --fix ─────────────────────────────────────────────────────────────────────
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO=sudo
echo "Disabling slow-boot services the kiosk doesn't need:"
for u in systemd-networkd-wait-online.service NetworkManager-wait-online.service ModemManager.service; do
    state=$(systemctl is-enabled "$u" 2>/dev/null)
    case "$state" in
        enabled|enabled-runtime|static|indirect)
            # mask: some of these are pulled in by other units even when "disabled"
            $SUDO systemctl disable "$u" >/dev/null 2>&1
            $SUDO systemctl mask "$u" >/dev/null 2>&1 && echo "   OK  masked $u (was $state)" ;;
        masked) echo "   OK  $u already masked" ;;
        *)      echo "   ..  $u not present" ;;
    esac
done
if [ -d /etc/cloud ]; then
    if [ -e /etc/cloud/cloud-init.disabled ]; then
        echo "   OK  cloud-init already disabled"
    else
        $SUDO touch /etc/cloud/cloud-init.disabled && echo "   OK  cloud-init disabled (/etc/cloud/cloud-init.disabled)"
    fi
else
    echo "   ..  cloud-init not present"
fi
echo
echo "To undo any of it:  sudo systemctl unmask <name>  /  sudo rm /etc/cloud/cloud-init.disabled"
echo "Reboot (or power-cycle), then run  $0  again to compare the boot time."
