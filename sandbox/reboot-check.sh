#!/bin/bash
# Why does `sudo reboot` hang on the pendulum Pi (Pi 5 + SSD HAT)?
#
#   ./reboot-check.sh         report only, changes nothing; saved to
#                             ~/.cache/barrett-pendulum/reboot-check.txt for pasting
#   ./reboot-check.sh --fix   apply the standard fixes -- asks before EACH one
#
# Two different failures look identical on the touch display (black screen):
#   A. stuck SHUTTING DOWN -- a service that won't stop. The previous boot's
#      journal then ends with "Stopping ..." / timeouts instead of reboot.target.
#   B. stuck STARTING -- the Pi 5 bootloader can't find the NVMe SSD on a warm
#      reboot and waits (its diagnostic screen only appears on HDMI, never on
#      the DSI touch display). A power-cycle resets the SSD, so that works.
#
# --fix, each step confirmed:
#   1. systemd: cap how long shutdown waits for a stuck service (15 s, was 90 s+)
#   2. bootloader: update to the latest release (rpi-eeprom-update -a)
#   3. bootloader: BOOT_ORDER=0xf416 (NVMe first, then SD, USB) + PCIE_PROBE=1
#      (probe the PCIe slot even for SSD HATs without the HAT+ ID EEPROM)
# Bootloader changes are flashed on the next reboot/power-cycle.
set -u
FIX=0; [ "${1:-}" = "--fix" ] && FIX=1
OUT_DIR="$HOME/.cache/barrett-pendulum"; mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/reboot-check.txt"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO=sudo

report() {
    . /etc/os-release 2>/dev/null
    echo "== $(date '+%F %T')  ${PRETTY_NAME:-?}  $(uname -r)  $(tr -d '\0' 2>/dev/null </proc/device-tree/model)"
    echo
    echo "== Recorded boots (a boot missing between two = one that never started)"
    journalctl --list-boots --no-pager 2>/dev/null | tail -6
    echo
    echo "== How the PREVIOUS boot ended (last 30 lines)"
    echo "   ends with 'reboot.target' / 'Rebooting' = shut down fine -> the hang was on the way back up (B)"
    echo "   ends with 'Stopping ...' / 'timed out'   = stuck shutting down (A)"
    journalctl -b -1 -n 30 --no-pager -o short-monotonic 2>/dev/null \
        || echo "   (no previous-boot journal -- persistent journald logging may be off)"
    echo
    echo "== Shutdown problems in the previous boot"
    journalctl -b -1 --no-pager 2>/dev/null \
        | grep -i -E 'timed out|timeout|failed to stop|still around|watchdog|killing|hung' | tail -10 \
        || true
    echo
    echo "== Shutdown stop timeout"
    systemctl show -p DefaultTimeoutStopUSec 2>/dev/null
    echo
    echo "== Storage"
    lsblk -d -o NAME,MODEL,SIZE,TRAN 2>/dev/null
    echo "   root is on: $(findmnt -n -o SOURCE /)"
    echo
    echo "== Pi 5 bootloader (EEPROM)"
    if command -v rpi-eeprom-update >/dev/null 2>&1; then
        $SUDO rpi-eeprom-update 2>&1 | sed 's/^/   /'
        echo "   -- config:"
        $SUDO rpi-eeprom-config 2>/dev/null | grep -E 'BOOT_ORDER|PCIE_PROBE|POWER_OFF_ON_HALT|NET_INSTALL' | sed 's/^/   /' \
            || echo "   (no BOOT_ORDER / PCIE_PROBE set)"
    else
        echo "   rpi-eeprom tools not installed (--fix offers to install them)"
    fi
}

if [ $FIX = 0 ]; then
    report 2>&1 | tee "$OUT"
    echo
    echo "Saved to $OUT -- paste it, or run  $0 --fix"
    exit 0
fi

ask() { printf '\n%s [y/N] ' "$1"; read -r ans; [ "$ans" = y ] || [ "$ans" = Y ]; }

# 1. shutdown timeout ---------------------------------------------------------
if ask "1. Cap shutdown's wait for a stuck service at 15 s (instead of 90 s+ each)?"; then
    $SUDO mkdir -p /etc/systemd/system.conf.d
    printf '[Manager]\nDefaultTimeoutStopSec=15s\n' \
        | $SUDO tee /etc/systemd/system.conf.d/10-pendulum-shutdown.conf >/dev/null
    $SUDO systemctl daemon-reexec && echo "   OK  DefaultTimeoutStopSec=15s"
    echo "   undo: sudo rm /etc/systemd/system.conf.d/10-pendulum-shutdown.conf"
fi

# 2 + 3. bootloader --------------------------------------------------------------
if ! command -v rpi-eeprom-update >/dev/null 2>&1; then
    if ask "Bootloader tools (rpi-eeprom) aren't installed. Install them?"; then
        $SUDO apt-get install -y rpi-eeprom || { echo "   XX  install failed"; exit 1; }
    else
        echo "Skipping the bootloader steps."; exit 0
    fi
fi

echo; $SUDO rpi-eeprom-update 2>&1 | sed 's/^/   /'
if ask "2. Update the Pi 5 bootloader to the latest release (flashed on next reboot)?"; then
    $SUDO rpi-eeprom-update -a && echo "   OK  update staged"
fi

cur=$($SUDO rpi-eeprom-config 2>/dev/null)
need=0
echo "$cur" | grep -q '^BOOT_ORDER=0xf416' || need=1
echo "$cur" | grep -q '^PCIE_PROBE=1' || need=1
if [ $need = 1 ]; then
    echo; echo "   current:"; echo "$cur" | sed 's/^/      /'
    if ask "3. Set BOOT_ORDER=0xf416 (NVMe first) and PCIE_PROBE=1?"; then
        tmp=$(mktemp)
        echo "$cur" | grep -v -E '^(BOOT_ORDER|PCIE_PROBE)=' > "$tmp"
        printf 'BOOT_ORDER=0xf416\nPCIE_PROBE=1\n' >> "$tmp"
        $SUDO rpi-eeprom-config --apply "$tmp" && echo "   OK  config staged"
        rm -f "$tmp"
    fi
else
    echo "   OK  BOOT_ORDER=0xf416 and PCIE_PROBE=1 already set"
fi

echo
echo "Bootloader changes flash during the next restart. Do this one as a"
echo "POWER-CYCLE (unplug 10 s) so it starts clean; after that, test  sudo reboot"
echo "and give it ~1 minute."
