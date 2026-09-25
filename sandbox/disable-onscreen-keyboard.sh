#!/bin/bash
# Turn off every on-screen keyboard on the pendulum Pi, for good.
#
#   ./disable-onscreen-keyboard.sh           find + disable (run as the desktop user,
#                                            not with sudo; it uses sudo where needed)
#   ./disable-onscreen-keyboard.sh --check   only report what's there; changes nothing
#
# Covers xvkbd, onboard, squeekboard, wvkbd and GNOME's built-in keyboard:
#   1. the "screen keyboard" accessibility setting (GNOME's keyboard AND squeekboard obey it)
#   2. login autostart entries (~/.config/autostart, /etc/xdg/autostart -> Hidden=true override)
#   3. compositor respawn lists (labwc / wayfire autostart) -- these relaunch a
#      keyboard straight after `pkill`, which is why killing it never stuck
#   4. systemd user services
#   5. the packages: uninstalled only when apt would remove nothing else
#   6. whatever is still running
# Every file it edits gets a .bak-keyboard backup first. Reboot afterwards.
set -u
NAMES='xvkbd|onboard|squeekboard|wvkbd|matchbox-keyboard|florence|caribou'
# Running processes: match the program name only (".../onboard ..."), not any
# command line that merely mentions one of the names.
PROC_RE="(^|/)($NAMES)[^ /]*( |\$)"
PKGS="xvkbd onboard onboard-common onboard-data squeekboard wvkbd matchbox-keyboard florence"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m  %s\n' "$*"; }
info() { printf '    ..  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
act()  { if [ $CHECK = 1 ]; then info "(--check) would: $*"; return 1; fi; return 0; }
backup() { [ -e "$1" ] && [ ! -e "$1.bak-keyboard" ] && cp -p "$1" "$1.bak-keyboard"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as the desktop user (not root/sudo) so it edits YOUR session's settings." >&2
    exit 1
fi
gs() {   # gsettings with a session bus even over SSH; ignore the apps' in-process backend
    if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then env -u GSETTINGS_BACKEND gsettings "$@"
    else env -u GSETTINGS_BACKEND dbus-run-session -- gsettings "$@"; fi
}

. /etc/os-release 2>/dev/null
say "System: ${PRETTY_NAME:-?} | session ${XDG_SESSION_TYPE:-none (SSH?)} | desktop ${XDG_CURRENT_DESKTOP:-?}"

# ── what is running now, and what started it ────────────────────────────────
say "Running keyboards (and their parent = what launches them)"
found_running=0
for p in $(pgrep -f -i "$PROC_RE"); do
    [ "$p" = "$$" ] && continue
    cmd=$(ps -o cmd= -p "$p" 2>/dev/null) || continue
    case "$cmd" in *disable-onscreen-keyboard*|*pgrep*) continue ;; esac
    found_running=1
    ppid=$(ps -o ppid= -p "$p" | tr -d ' ')
    info "pid $p ($(ps -o user= -p "$p")): $cmd"
    info "   parent $ppid: $(ps -o cmd= -p "$ppid" 2>/dev/null)"
done
[ $found_running = 0 ] && ok "none running right now"

# ── 1. accessibility "screen keyboard" setting ───────────────────────────────
say "1. Screen-keyboard setting (GNOME keyboard + squeekboard)"
if command -v gsettings >/dev/null 2>&1; then
    cur=$(gs get org.gnome.desktop.a11y.applications screen-keyboard-enabled 2>/dev/null)
    info "currently: ${cur:-n/a}"
    if [ "$cur" = "true" ] && act "set screen-keyboard-enabled false"; then
        gs set org.gnome.desktop.a11y.applications screen-keyboard-enabled false && ok "set to false"
    elif [ "$cur" = "false" ]; then ok "already off"; fi
else
    info "gsettings not installed -- skipping"
fi

# ── 2. XDG autostart entries ────────────────────────────────────────────────
say "2. Login autostart entries"
mkdir -p "$HOME/.config/autostart"
hits=0
for f in /etc/xdg/autostart/*.desktop "$HOME"/.config/autostart/*.desktop; do
    [ -e "$f" ] || continue
    grep -q -i -E "$NAMES" "$f" || continue
    case "$(basename "$f")" in barrett-*) continue ;; esac     # ours
    hits=1
    user_copy="$HOME/.config/autostart/$(basename "$f")"
    if grep -q '^Hidden=true' "$user_copy" 2>/dev/null; then ok "already hidden: $(basename "$f")"; continue; fi
    info "found: $f"
    if act "hide $(basename "$f") via $user_copy (Hidden=true)"; then
        if [ "$f" != "$user_copy" ]; then cp "$f" "$user_copy"; else backup "$f"; fi
        sed -i '/^Hidden=/d; /^X-GNOME-Autostart-enabled=/d' "$user_copy"
        printf 'Hidden=true\nX-GNOME-Autostart-enabled=false\n' >> "$user_copy"
        ok "hidden: $user_copy"
    fi
done
[ $hits = 0 ] && ok "no keyboard autostart entries"

# ── 3. compositor autostart / respawn lists ─────────────────────────────────
say "3. Compositor autostart (labwc / wayfire) -- relaunches keyboards after pkill"
comp_hits=0
# labwc: the user file replaces the system one, so seed it from the system copy first.
if [ -e /etc/xdg/labwc/autostart ] || [ -e "$HOME/.config/labwc/autostart" ]; then
    uf="$HOME/.config/labwc/autostart"
    src="$uf"; [ -e "$uf" ] || src=/etc/xdg/labwc/autostart
    if grep -q -i -E "^[^#]*($NAMES)" "$src"; then
        comp_hits=1
        grep -n -i -E "^[^#]*($NAMES)" "$src" | sed 's/^/    ..  labwc: /'
        if act "comment those lines out in $uf"; then
            mkdir -p "$(dirname "$uf")"
            [ -e "$uf" ] || cp /etc/xdg/labwc/autostart "$uf"
            backup "$uf"
            sed -i -E "s/^([^#]*($NAMES).*)$/# disabled by disable-onscreen-keyboard.sh: \1/I" "$uf"
            ok "labwc autostart updated: $uf"
        fi
    fi
fi
for wf in "$HOME/.config/wayfire.ini"; do
    [ -e "$wf" ] || continue
    if grep -q -i -E "^[^#]*($NAMES)" "$wf"; then
        comp_hits=1
        grep -n -i -E "^[^#]*($NAMES)" "$wf" | sed 's/^/    ..  wayfire: /'
        if act "comment those lines out in $wf"; then
            backup "$wf"
            sed -i -E "s/^([^#]*($NAMES).*)$/# disabled by disable-onscreen-keyboard.sh: \1/I" "$wf"
            ok "wayfire.ini updated"
        fi
    fi
done
[ $comp_hits = 0 ] && ok "no keyboard in labwc/wayfire autostart"

# ── 4. systemd user services ────────────────────────────────────────────────
say "4. systemd user services"
# On-screen keyboard names only -- NOT a bare "keyboard" match, which would hit
# e.g. org.gnome.SettingsDaemon.Keyboard (physical keyboard layouts).
units=$(systemctl --user list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -i -E "$NAMES")
if [ -z "$units" ]; then ok "none"; fi
for u in $units; do
    info "found: $u ($(systemctl --user is-enabled "$u" 2>/dev/null))"
    if act "systemctl --user disable --now + mask $u"; then
        systemctl --user disable --now "$u" 2>/dev/null
        systemctl --user mask "$u" 2>/dev/null && ok "masked $u"
    fi
done

# ── 5. packages ─────────────────────────────────────────────────────────────
say "5. Installed keyboard packages"
installed=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 && installed="$installed $p"; done
if [ -z "$installed" ]; then
    ok "none installed"
else
    info "installed:$installed"
    # Only purge if apt would remove nothing beyond these packages (squeekboard
    # can be tied to the desktop meta-packages on Raspberry Pi OS).
    extra=$(apt-get -s purge $installed 2>/dev/null | awk '/^Purg|^Remv/{print $2}' \
            | grep -v -x -E "$(echo $PKGS | tr ' ' '|')")
    if [ -n "$extra" ]; then
        warn "NOT uninstalling: apt would also remove: $(echo $extra | tr '\n' ' ')"
        warn "(steps 1-4 already stop them from starting, so that's fine)"
    elif act "sudo apt-get purge -y$installed"; then
        sudo apt-get purge -y $installed >/dev/null && ok "purged:$installed"
    fi
fi

# ── 6. stop whatever is still running ───────────────────────────────────────
say "6. Stop running keyboards"
if act "stop running keyboard processes"; then
    pkill -f -i "$PROC_RE" 2>/dev/null
    sleep 1
    still=$(pgrep -a -f -i "$PROC_RE" | grep -v -E 'disable-onscreen-keyboard|pgrep')
    if [ -z "$still" ]; then ok "none running"
    else warn "still running (probably respawned; the reboot applies steps 1-4):"; echo "$still" | sed 's/^/        /'; fi
fi

say "Done"
if [ $CHECK = 1 ]; then
    echo "    Nothing was changed (--check). Run without --check to apply."
else
    echo "    Reboot to finish:  sudo reboot"
    echo "    If a keyboard still appears after the reboot, run:  $0 --check"
    echo "    and send the output -- the 'parent' line shows what launched it."
fi
