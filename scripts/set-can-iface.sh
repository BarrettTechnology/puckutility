#!/bin/bash
# Manually assign a specific CAN adapter to a specific SocketCAN interface name
# (can0..can3) and bring it up. Lets you test any of the four apps (P4-Checkout,
# PuckUtility, PuckTuner, the calibrate/factory menus) against a chosen adapter
# on a chosen interface -- all four select the port by SocketCAN name on Linux
# (GUI "CAN Port" submenu can0..can3, or --can on the CLI).
#
# Adapters are told apart by their kernel driver, which is unambiguous:
#     CANable / CandleLight  ->  gs_usb
#     Peak PCAN-USB          ->  peak_usb
#
# Bring-up config mirrors reset_can.sh: CAN-FD (1 Mbit nominal / 5 Mbit data),
# falling back to classic 1 Mbit for adapters without FD.
#
# Usage:
#   set-can-iface.sh list                       # show every CAN adapter present
#   set-can-iface.sh <canable|peak> <0..3> [bitrate] [dbitrate]
#
# Examples:
#   set-can-iface.sh canable 0                  # CANable -> can0 (FD 1M/5M)
#   set-can-iface.sh peak 1                     # Peak    -> can1
#   set-can-iface.sh canable 2 500000           # classic 500 kbit
#
# Disambiguation: if more than one adapter of the requested type is plugged in,
# pick which physical one to move with  CAN_FROM=<current-name>  e.g.
#   CAN_FROM=can5 set-can-iface.sh canable 3
#
# If the target name is already taken by a *different* adapter, that adapter is
# parked at the next free can0..can9 slot (so swapping can0<->can1 just works).
set -e

BITRATE_DEFAULT=1000000
DBITRATE_DEFAULT=5000000

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO=sudo; fi

# --- helpers -----------------------------------------------------------------
# A CAN netdev has ARPHRD_CAN (280) as its link type, regardless of its name.
is_can() { [ "$(cat "/sys/class/net/$1/type" 2>/dev/null)" = "280" ]; }

driver_of() {
    basename "$(readlink -f "/sys/class/net/$1/device/driver" 2>/dev/null)" 2>/dev/null
}

label_of() {
    case "$(driver_of "$1")" in
        gs_usb)   echo "CANable" ;;
        peak_usb) echo "Peak PCAN" ;;
        "")       echo "?" ;;
        *)        driver_of "$1" ;;
    esac
}

all_can_ifaces() {
    local p ifc
    for p in /sys/class/net/*; do
        ifc=$(basename "$p")
        is_can "$ifc" && echo "$ifc"
    done
}

# First free canN (N in 0..9) not currently used by any netdev.
first_free_can() {
    local n
    for n in 0 1 2 3 4 5 6 7 8 9; do
        [ -e "/sys/class/net/can$n" ] || { echo "can$n"; return; }
    done
    echo "ERR: no free can0..can9 slot" >&2; exit 1
}

list_adapters() {
    local found=0 ifc
    printf "%-10s %-12s %-7s %s\n" "IFACE" "ADAPTER" "STATE" "USB PATH"
    for ifc in $(all_can_ifaces); do
        found=1
        printf "%-10s %-12s %-7s %s\n" \
            "$ifc" "$(label_of "$ifc")" \
            "$(cat "/sys/class/net/$ifc/operstate" 2>/dev/null)" \
            "$(basename "$(readlink -f "/sys/class/net/$ifc/device" 2>/dev/null)")"
    done
    [ "$found" = 0 ] && echo "(no CAN adapters present)"
}

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# Configure + bring up an interface (mirrors reset_can.sh, with overrides).
bring_up() {
    local ifc="$1" bitrate="$2" dbitrate="$3"
    $SUDO ip link set "$ifc" down 2>/dev/null || true
    $SUDO ip link set "$ifc" type can bitrate "$bitrate" fd on dbitrate "$dbitrate" \
        || $SUDO ip link set "$ifc" type can bitrate "$bitrate"
    $SUDO ip link set "$ifc" type can restart-ms 100 2>/dev/null || true
    $SUDO ip link set "$ifc" txqueuelen 1000
    $SUDO ip link set "$ifc" up
}

# --- argument parsing --------------------------------------------------------
case "${1:-}" in
    ""|-h|--help|help) usage 0 ;;
    list|ls)           list_adapters; exit 0 ;;
esac

ADAPTER="$1"
INDEX="$2"
BITRATE="${3:-$BITRATE_DEFAULT}"
DBITRATE="${4:-$DBITRATE_DEFAULT}"

case "$(echo "$ADAPTER" | tr '[:upper:]' '[:lower:]')" in
    canable|candlelight|gs_usb|gs)        WANT_DRV=gs_usb;   WANT_LABEL="CANable" ;;
    peak|pcan|peakcan|peak_usb|peak-usb)  WANT_DRV=peak_usb; WANT_LABEL="Peak PCAN" ;;
    *) echo "ERROR: unknown adapter '$ADAPTER' (use canable|peak, or 'list')" >&2; usage 1 ;;
esac

case "$INDEX" in
    0|1|2|3) TARGET="can$INDEX" ;;
    *) echo "ERROR: interface index must be 0, 1, 2 or 3 (got '$INDEX')" >&2; usage 1 ;;
esac

# --- find the source adapter -------------------------------------------------
matches=""
for ifc in $(all_can_ifaces); do
    [ "$(driver_of "$ifc")" = "$WANT_DRV" ] && matches="$matches $ifc"
done
matches="${matches# }"

if [ -z "$matches" ]; then
    echo "ERROR: no $WANT_LABEL ($WANT_DRV) adapter is plugged in." >&2
    echo "Currently present:" >&2; list_adapters >&2
    exit 1
fi

if [ -n "$CAN_FROM" ]; then
    case " $matches " in
        *" $CAN_FROM "*) SRC="$CAN_FROM" ;;
        *) echo "ERROR: CAN_FROM='$CAN_FROM' is not a $WANT_LABEL adapter (have:$matches)" >&2; exit 1 ;;
    esac
elif [ "$(echo "$matches" | wc -w)" -gt 1 ]; then
    echo "ERROR: multiple $WANT_LABEL adapters present:$matches" >&2
    echo "Pick one with CAN_FROM=<name>, e.g.  CAN_FROM=${matches%% *} $0 $ADAPTER $INDEX" >&2
    exit 1
else
    SRC="$matches"
fi

# --- park whatever currently holds the target name (if it's not our source) --
if [ "$SRC" != "$TARGET" ] && [ -e "/sys/class/net/$TARGET" ]; then
    PARK="$(first_free_can)"
    echo "Note: $TARGET is in use by $(label_of "$TARGET") -> parking it at $PARK"
    $SUDO ip link set "$TARGET" down 2>/dev/null || true
    $SUDO ip link set "$TARGET" name "$PARK"
fi

# --- rename source -> target (link must be down to rename) -------------------
if [ "$SRC" != "$TARGET" ]; then
    $SUDO ip link set "$SRC" down 2>/dev/null || true
    $SUDO ip link set "$SRC" name "$TARGET"
fi

bring_up "$TARGET" "$BITRATE" "$DBITRATE"

echo "OK: $WANT_LABEL is now $TARGET (bitrate $BITRATE, dbitrate $DBITRATE)."
echo "Point any app at it:  GUI Configure -> CAN Port -> $TARGET,  or  --can $TARGET"
