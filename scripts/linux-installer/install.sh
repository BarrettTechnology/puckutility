#!/bin/bash
# Puck Utility installer for Ubuntu 20.04 -> 26.04 (x86_64).
#
# Normally run from the self-extracting PuckUtilityApp-lin-<ver>-installer.run;
# works the same from an unpacked copy (./install.sh). Installs the app and,
# as options, the CAN adapter support packages:
#
#   Peak PCAN-USB   mainline peak_usb driver + USB permissions
#   CANable         mainline gs_usb driver + USB/DFU permissions + dfu-util
#
# Both adapters come up automatically as can0, can1, ... when plugged in
# (CAN-FD 1M/5M, classic 1M fallback). Re-running the installer changes the
# selection: a deselected adapter's package is removed.
#
# Options:
#   --peak / --no-peak         select / deselect Peak PCAN-USB support
#   --canable / --no-canable   select / deselect CANable support
#   -y, --yes                  no questions: the defaults (both adapters) or the
#                              flags above
#   --text                     text questions even when a desktop is available
#   --no-kernel-check          skip the kernel driver check
#   --uninstall                remove Puck Utility and the CAN support packages
#   --dry-run                  print what would be done, change nothing
#   -h, --help
set -euo pipefail

VERSION="@VERSION@"
HERE="$(cd "$(dirname "$0")" && pwd)"
SELF="$HERE/$(basename "$0")"
DEBS="$HERE/debs"
TITLE="Puck Utility $VERSION installer"

APP_PKG=puckutilityapp
CAN_PKGS=(puck-can-common puck-can-peak puck-can-canable)
# Other apps that use puck-can-common; keep it while one of them is installed.
SIBLING_APPS=(pucktunerapp p4checkoutapp)

PEAK=""        # 1 / 0 / "" (= not given on the command line)
CANABLE=""
ASSUME_YES=false
FORCE_TEXT=false
KERNEL_CHECK=true
UNINSTALL=false
DRY_RUN=false
ELEVATED=false

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --peak)            PEAK=1 ;;
        --no-peak)         PEAK=0 ;;
        --canable)         CANABLE=1 ;;
        --no-canable)      CANABLE=0 ;;
        -y|--yes)          ASSUME_YES=true ;;
        --text)            FORCE_TEXT=true ;;
        --no-kernel-check) KERNEL_CHECK=false ;;
        --uninstall)       UNINSTALL=true ;;
        --dry-run)         DRY_RUN=true ;;
        --elevated)        ELEVATED=true ;;   # internal: the re-exec as root
        -h|--help)         usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

# --- output -------------------------------------------------------------------
say()  { echo "$*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }
run()  {   # run a (root) command, or just show it under --dry-run
    if $DRY_RUN; then echo "+ $*"; else "$@"; fi
}

have() { command -v "$1" >/dev/null 2>&1; }
gui_ok() {
    ! $FORCE_TEXT && have zenity && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }
}
tty_ok() { [ -t 0 ] && [ -t 1 ]; }

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed'; }

in_container() {
    [ -f /.dockerenv ] || [ -f /run/.containerenv ] && return 0
    have systemd-detect-virt && systemd-detect-virt --container --quiet 2>/dev/null
}

# --- platform -----------------------------------------------------------------
check_platform() {
    [ "$(uname -s)" = Linux ] || die "this installer is for Linux."
    have dpkg && have apt-get || die "this installer needs a Debian/Ubuntu system (dpkg + apt)."
    local arch; arch=$(dpkg --print-architecture)
    [ "$arch" = amd64 ] || die "Puck Utility is built for amd64 (x86_64); this system is $arch."

    # Derivatives (Mint, Pop!_OS, elementary, ...) report the Ubuntu base in
    # UBUNTU_CODENAME, so check that rather than ID / VERSION_ID.
    local id="" ver="" codename="" pretty=""
    if [ -r /etc/os-release ]; then
        id=$(. /etc/os-release; echo "${ID:-}")
        ver=$(. /etc/os-release; echo "${VERSION_ID:-}")
        codename=$(. /etc/os-release; echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")
        pretty=$(. /etc/os-release; echo "${PRETTY_NAME:-}")
    fi
    case "$codename" in
        focal|jammy|noble|resolute) ;;                         # LTS 20.04 22.04 24.04 26.04
        groovy|hirsute|impish|kinetic|lunar|mantic|oracular|plucky|questing) ;;  # interim releases
        *)
            warn "${pretty:-this system} is not Ubuntu 20.04 - 26.04 (or a derivative of it)."
            if ! $ASSUME_YES; then confirm "Install anyway?" || exit 1; fi
            ;;
    esac
    say "System: ${pretty:-$id $ver} (${codename:-unknown base}), $arch"
}

# --- questions ----------------------------------------------------------------
confirm() {   # confirm <question>: yes/no in the GUI, a tty, or no (no way to ask)
    if gui_ok; then
        zenity --question --title="$TITLE" --width=420 --text="$1" 2>/dev/null
    elif tty_ok; then
        local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]
    else
        return 1
    fi
}

# USB vendor/product present right now? Labels the choices "(plugged in)".
usb_present() {
    local f
    for f in /sys/bus/usb/devices/*/idVendor; do
        [ -r "$f" ] || continue
        [ "$(cat "$f")" = "$1" ] || continue
        [ -z "${2:-}" ] && return 0
        [ "$(cat "${f%idVendor}idProduct" 2>/dev/null)" = "$2" ] && return 0
    done
    return 1
}

ask_components() {
    # The command line wins; otherwise default to both adapters.
    local peak_def=1 canable_def=1
    [ -n "$PEAK" ] && peak_def=$PEAK
    [ -n "$CANABLE" ] && canable_def=$CANABLE
    if $ASSUME_YES || { [ -n "$PEAK" ] && [ -n "$CANABLE" ]; }; then
        PEAK=$peak_def; CANABLE=$canable_def; return
    fi

    local peak_lbl="Peak PCAN-USB adapter"
    local canable_lbl="CANable / CandleLight adapter"
    usb_present 0c72 && peak_lbl="$peak_lbl  (plugged in)"
    { usb_present 1d50 606f || usb_present 0483 df11; } && canable_lbl="$canable_lbl  (plugged in)"
    local peak_desc="mainline peak_usb driver, USB permissions"
    local canable_desc="mainline gs_usb driver, USB + firmware-flashing (DFU) permissions, dfu-util"

    if gui_ok; then
        local tf_p=FALSE tf_c=FALSE out
        [ "$peak_def" = 1 ] && tf_p=TRUE
        [ "$canable_def" = 1 ] && tf_c=TRUE
        out=$(zenity --list --checklist --title="$TITLE" --width=720 --height=300 \
                --text="Puck Utility $VERSION will be installed. Choose the CAN adapters to set up:" \
                --column=" " --column="Adapter" --column="Installs" \
                --hide-column=4 --print-column=4 --column=key --separator=" " \
                "$tf_p" "$peak_lbl" "$peak_desc" peak \
                "$tf_c" "$canable_lbl" "$canable_desc" canable 2>/dev/null) \
            || { say "Cancelled."; exit 1; }
        PEAK=0; CANABLE=0
        [[ " $out " == *" peak "* ]] && PEAK=1
        [[ " $out " == *" canable "* ]] && CANABLE=1
    elif tty_ok && have whiptail; then
        local on_p=OFF on_c=OFF out
        [ "$peak_def" = 1 ] && on_p=ON
        [ "$canable_def" = 1 ] && on_c=ON
        out=$(whiptail --title "$TITLE" --separate-output --checklist \
                "Puck Utility will be installed. Choose the CAN adapters to set up\n(space toggles, Enter confirms):" \
                14 76 2 \
                peak    "$peak_lbl"    "$on_p" \
                canable "$canable_lbl" "$on_c" 3>&1 1>&2 2>&3) \
            || { say "Cancelled."; exit 1; }
        PEAK=0; CANABLE=0
        grep -qx peak    <<<"$out" && PEAK=1
        grep -qx canable <<<"$out" && CANABLE=1
    elif tty_ok; then
        local a
        read -r -p "Set up $peak_lbl? [$([ "$peak_def" = 1 ] && echo Y/n || echo y/N)] " a
        PEAK=$peak_def; [[ "$a" =~ ^[Yy] ]] && PEAK=1; [[ "$a" =~ ^[Nn] ]] && PEAK=0
        read -r -p "Set up $canable_lbl? [$([ "$canable_def" = 1 ] && echo Y/n || echo y/N)] " a
        CANABLE=$canable_def; [[ "$a" =~ ^[Yy] ]] && CANABLE=1; [[ "$a" =~ ^[Nn] ]] && CANABLE=0
    else
        PEAK=$peak_def; CANABLE=$canable_def
    fi
}

# --- root ---------------------------------------------------------------------
# Questions are asked as the user (their display, their tty); only the
# install itself runs as root, by re-running this script with the answers.
elevate() {
    local args=(--elevated --yes)
    $UNINSTALL && args+=(--uninstall)
    [ "$PEAK" = 1 ] && args+=(--peak) || args+=(--no-peak)
    [ "$CANABLE" = 1 ] && args+=(--canable) || args+=(--no-canable)
    $KERNEL_CHECK || args+=(--no-kernel-check)

    if [ "$(id -u)" -eq 0 ]; then
        bash "$SELF" "${args[@]}"
    elif gui_ok && ! tty_ok && have pkexec; then
        # Launched without a terminal: pkexec asks for the password in a
        # desktop dialog, and a progress window shows the apt output.
        local log status
        log=$(mktemp /tmp/puckutility-install.XXXXXX.log)
        status=$(mktemp)
        { pkexec /bin/bash "$SELF" "${args[@]}" 2>&1; echo $? > "$status"; } | tee "$log" \
            | sed -u 's/^/# /' \
            | zenity --progress --pulsate --auto-close --no-cancel --width=520 \
                     --title="$TITLE" --text="Installing..." 2>/dev/null || true
        if [ "$(cat "$status")" = 0 ]; then
            zenity --text-info --title="$TITLE - done" --width=720 --height=420 \
                   --filename="$log" 2>/dev/null || true
        else
            zenity --text-info --title="$TITLE - FAILED" --width=720 --height=420 \
                   --filename="$log" 2>/dev/null || true
            exit 1
        fi
    elif have sudo; then
        say "Administrator rights are needed to install packages."
        sudo bash "$SELF" "${args[@]}"
    elif have pkexec; then
        pkexec /bin/bash "$SELF" "${args[@]}"
    else
        die "run this installer as root (no sudo or pkexec found)."
    fi
}

# --- the work (as root) -------------------------------------------------------
kmod_present() {   # kmod_present <kernel> <module>: built in, or a .ko on disk
    # Looked up on disk, not with modinfo: modinfo is in /sbin, often not on PATH.
    local kdir="/lib/modules/$1"
    grep -q "/$2\.ko" "$kdir/modules.builtin" 2>/dev/null && return 0
    [ -n "$(find "$kdir" -name "$2.ko*" -print -quit 2>/dev/null)" ]
}

kernel_extras() {   # echo linux-modules-extra-<kver> if a needed driver is missing
    $KERNEL_CHECK || return 0
    if in_container; then
        say "Kernel driver check skipped (running in a container)." >&2
        return 0
    fi
    local kver mods=() missing=() m
    kver=$(uname -r)
    [ "$PEAK" = 1 ] && mods+=(peak_usb)
    [ "$CANABLE" = 1 ] && mods+=(gs_usb)
    for m in "${mods[@]}"; do
        kmod_present "$kver" "$m" || missing+=("$m")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    # On Ubuntu the CAN USB drivers live in linux-modules-extra (absent on
    # cloud / minimal kernels).
    local pkg="linux-modules-extra-$kver"
    if apt-cache show "$pkg" >/dev/null 2>&1; then
        say "Kernel driver(s) ${missing[*]} not installed; adding $pkg." >&2
        echo "$pkg"
    else
        warn "kernel driver(s) ${missing[*]} are missing for kernel $kver and $pkg is not available."
        warn "Install your kernel's extra modules package (e.g. linux-generic), reboot, and re-run."
    fi
    return 0
}

do_install() {
    [ -f "$DEBS/${APP_PKG}_${VERSION}_amd64.deb" ] || die "package not found: $DEBS/${APP_PKG}_${VERSION}_amd64.deb"
    local pkgs=("$DEBS/${APP_PKG}_${VERSION}_amd64.deb" "$DEBS/puck-can-common_${VERSION}_all.deb")
    local remove=()
    if [ "$PEAK" = 1 ]; then pkgs+=("$DEBS/puck-can-peak_${VERSION}_all.deb")
    elif installed puck-can-peak; then remove+=(puck-can-peak); fi
    if [ "$CANABLE" = 1 ]; then pkgs+=("$DEBS/puck-can-canable_${VERSION}_all.deb")
    elif installed puck-can-canable; then remove+=(puck-can-canable); fi

    export DEBIAN_FRONTEND=noninteractive
    say "==> Updating the package lists"
    run apt-get update -q || warn "apt-get update failed; continuing with the cached lists."

    local extra; extra=$(kernel_extras)
    [ -n "$extra" ] && pkgs+=("$extra")

    if [ ${#remove[@]} -gt 0 ]; then
        say "==> Removing deselected adapter support: ${remove[*]}"
        run apt-get remove -y -q "${remove[@]}"
    fi
    say "==> Installing: $(for p in "${pkgs[@]}"; do basename "$p" .deb; done | tr '\n' ' ')"
    run apt-get install -y -q --allow-downgrades "${pkgs[@]}"

    if ! in_container && ! $DRY_RUN; then
        [ "$PEAK" = 1 ] && { modprobe peak_usb 2>/dev/null || warn "could not load peak_usb (a reboot into the new kernel may be needed)."; }
        [ "$CANABLE" = 1 ] && { modprobe gs_usb 2>/dev/null || warn "could not load gs_usb (a reboot into the new kernel may be needed)."; }
    fi

    say
    say "Puck Utility $VERSION installed."
    say "  Start it from the applications menu, or run:  PuckUtilityApp"
    [ "$PEAK" = 1 ]    && say "  Peak PCAN-USB:  set up (peak_usb)"
    [ "$CANABLE" = 1 ] && say "  CANable:        set up (gs_usb; flash firmware from Puck Utility's CANable menu)"
    [ "$PEAK$CANABLE" = 00 ] && say "  No CAN adapter selected; re-run the installer to add one."
    say "  Adapters come up as can0, can1, ... when plugged in (replug one that is already in)."
    if have ip && ! $DRY_RUN; then
        local now; now=$(ip -br link show type can 2>/dev/null || true)
        [ -n "$now" ] && { say "  CAN interfaces present now:"; sed 's/^/    /' <<<"$now"; }
    fi
    return 0
}

do_uninstall() {
    local pkgs=() p keep_common=false
    for p in "${SIBLING_APPS[@]}"; do
        if installed "$p"; then keep_common=true; fi
    done
    for p in "$APP_PKG" puck-can-peak puck-can-canable; do
        if installed "$p"; then pkgs+=("$p"); fi
    done
    if ! $keep_common && installed puck-can-common; then pkgs+=(puck-can-common); fi
    if [ ${#pkgs[@]} -eq 0 ]; then say "Puck Utility is not installed."; return; fi
    say "==> Removing: ${pkgs[*]}"
    DEBIAN_FRONTEND=noninteractive run apt-get remove -y -q "${pkgs[@]}"
    if $keep_common; then say "Kept puck-can-common: another puck app still uses it."; fi
    say "Puck Utility removed."
}

work() { if $UNINSTALL; then do_uninstall; else do_install; fi; }

# ==============================================================================
if $ELEVATED; then
    # The root half: the answers arrive as flags.
    work
    exit 0
fi

check_platform
if $UNINSTALL; then
    $ASSUME_YES || confirm "Remove Puck Utility and its CAN adapter support?" || exit 1
    PEAK=0; CANABLE=0
else
    ask_components
    say "Selected: Peak PCAN-USB $([ "$PEAK" = 1 ] && echo yes || echo no), CANable $([ "$CANABLE" = 1 ] && echo yes || echo no)"
fi
if $DRY_RUN; then work; else elevate; fi
