#!/usr/bin/env bash
# Build the CAN adapter support packages that the Linux installer offers as
# options, separately from the app package:
#
#   puck-can-common   SocketCAN auto bring-up (61-can-up.rules + can-up@.service,
#                     CAN-FD 1M/5M with classic 1M fallback), NetworkManager
#                     exclusion, can-utils, set-can-iface.sh
#   puck-can-peak     Peak PCAN-USB (mainline peak_usb driver): USB permissions
#   puck-can-canable  CANable / CandleLight (mainline gs_usb driver): USB
#                     permissions for the adapter and its STM32 DFU bootloader,
#                     dfu-util for flashing the bundled firmware
#
# The packages hold no app code, so any puck app (puckutility, pucktuner) can
# depend on the same ones. Files go under /usr/lib (udev and systemd read it on
# every Ubuntu 20.04 -> 26.04, split-usr or merged), NOT /etc: the older app
# debs own the same basenames under /etc, and an /etc file shadows the /usr/lib
# one of the same name, so the two never run twice and dpkg never sees a
# file conflict between them.
#
# Usage: scripts/build-can-debs.sh <version> <outdir>
set -e
cd "$(dirname "$0")/.."

VERSION="${1:?usage: build-can-debs.sh <version> <outdir>}"
OUTDIR="${2:?usage: build-can-debs.sh <version> <outdir>}"
MAINTAINER="Barrett Technology <bn@barrett.com>"
mkdir -p "$OUTDIR"

# --- helpers ------------------------------------------------------------------
# new_pkg <name>: fresh staging root; echoes its path.
new_pkg() {
    local root="$OUTDIR/$1_${VERSION}_all"
    rm -rf "$root"
    mkdir -p "$root/DEBIAN"
    echo "$root"
}

# control <root> <name> <depends> <summary> <long description...>
control() {
    local root="$1" name="$2" depends="$3" summary="$4"; shift 4
    local size
    size=$(du -sk --exclude=DEBIAN "$root" | cut -f1)
    {
        echo "Package: $name"
        echo "Version: $VERSION"
        echo "Architecture: all"
        echo "Section: misc"
        echo "Priority: optional"
        echo "Installed-Size: $size"
        echo "Maintainer: $MAINTAINER"
        echo "Homepage: https://barrett.com"
        echo "Depends: $depends"
        echo "Description: $summary"
        for line in "$@"; do echo " $line"; done
    } > "$root/DEBIAN/control"
}

script() {  # script <root> <postinst|postrm>  (body on stdin)
    cat > "$1/DEBIAN/$2"
    chmod 755 "$1/DEBIAN/$2"
}

build() {
    dpkg-deb --build --root-owner-group "$1" >/dev/null
    rm -rf "$1"
    echo "Built: $1.deb"
}

# Re-reads the udev rules and replays "add" for adapters already plugged in,
# so the install takes effect without a replug. Best-effort: udev/systemd are
# absent in a chroot or container image build.
UDEV_REFRESH='udevadm control --reload-rules 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true'

# ==============================================================================
# puck-can-common
# ==============================================================================
ROOT=$(new_pkg puck-can-common)
install -Dm644 scripts/61-can-up.rules   "$ROOT/usr/lib/udev/rules.d/61-can-up.rules"
install -Dm644 scripts/can-up@.service   "$ROOT/usr/lib/systemd/system/can-up@.service"
install -Dm755 scripts/set-can-iface.sh  "$ROOT/usr/bin/set-can-iface.sh"
control "$ROOT" puck-can-common "can-utils, iproute2, udev" \
    "SocketCAN bring-up for Barrett P4 Puck tools" \
    "Brings every SocketCAN interface (can0, can1, ...) up automatically when" \
    "its adapter is plugged in: CAN-FD at 1 Mbit/s nominal and 5 Mbit/s data," \
    "falling back to classic 1 Mbit/s for adapters without FD. Keeps" \
    "NetworkManager away from CAN interfaces. Install puck-can-peak and/or" \
    "puck-can-canable for the adapter-specific rules."
script "$ROOT" postinst <<EOF
#!/bin/sh
set -e
# Written here rather than shipped: the older app debs write and delete this
# same file from their maintainer scripts, so it cannot be dpkg-owned.
mkdir -p /etc/NetworkManager/conf.d
printf '[keyfile]\nunmanaged-devices=type:can\n' > /etc/NetworkManager/conf.d/99-puck-can.conf
if [ -d /run/systemd/system ] && systemctl is-active --quiet NetworkManager 2>/dev/null; then
    systemctl reload NetworkManager 2>/dev/null || true
fi
$UDEV_REFRESH
# Bring up any CAN interface that is already present.
udevadm trigger --action=add --subsystem-match=net --attr-match=type=280 2>/dev/null || true
exit 0
EOF
script "$ROOT" postrm <<EOF
#!/bin/sh
set -e
case "\$1" in
remove|purge)
    # pucktuner / P4-Checkout write the same file; leave it while one remains.
    keep=
    for p in pucktunerapp p4checkoutapp; do
        if dpkg-query -W -f='\${Status}' "\$p" 2>/dev/null | grep -q 'ok installed'; then keep=1; fi
    done
    [ -n "\$keep" ] || rm -f /etc/NetworkManager/conf.d/99-puck-can.conf
    $UDEV_REFRESH
    ;;
esac
exit 0
EOF
build "$ROOT"

# ==============================================================================
# puck-can-peak
# ==============================================================================
ROOT=$(new_pkg puck-can-peak)
install -Dm644 scripts/60-can.rules "$ROOT/usr/lib/udev/rules.d/60-can.rules"
control "$ROOT" puck-can-peak "puck-can-common (= $VERSION)" \
    "Peak PCAN-USB adapter support for Barrett P4 Puck tools" \
    "Uses the mainline Linux peak_usb SocketCAN driver (no out-of-tree driver)." \
    "Grants the logged-in user access to Peak USB adapters (VID 0c72)."
script "$ROOT" postinst <<EOF
#!/bin/sh
set -e
$UDEV_REFRESH
modprobe peak_usb 2>/dev/null || true
udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=0c72 2>/dev/null || true
exit 0
EOF
script "$ROOT" postrm <<EOF
#!/bin/sh
set -e
$UDEV_REFRESH
exit 0
EOF
build "$ROOT"

# ==============================================================================
# puck-can-canable
# ==============================================================================
ROOT=$(new_pkg puck-can-canable)
install -Dm644 scripts/90-canable.rules "$ROOT/usr/lib/udev/rules.d/90-canable.rules"
control "$ROOT" puck-can-canable "puck-can-common (= $VERSION), dfu-util" \
    "CANable / CandleLight adapter support for Barrett P4 Puck tools" \
    "Uses the mainline Linux gs_usb SocketCAN driver. Grants the logged-in user" \
    "access to CandleLight adapters (1d50:606f) and to the STM32 DFU bootloader" \
    "(0483:df11), and installs dfu-util, so Puck Utility can flash the CANable" \
    "firmware without sudo."
script "$ROOT" postinst <<EOF
#!/bin/sh
set -e
$UDEV_REFRESH
modprobe gs_usb 2>/dev/null || true
udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=1d50 2>/dev/null || true
udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=0483 2>/dev/null || true
exit 0
EOF
script "$ROOT" postrm <<EOF
#!/bin/sh
set -e
$UDEV_REFRESH
exit 0
EOF
build "$ROOT"
