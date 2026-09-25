#!/bin/bash
# Turn off every on-screen keyboard on the pendulum Pi, for good.
#
#   ./disable-onscreen-keyboard.sh           find + disable (run as the desktop user,
#                                            not with sudo; it uses sudo where needed)
#   ./disable-onscreen-keyboard.sh --check   only report what's there; changes nothing
#   ./disable-onscreen-keyboard.sh --block-gnome-too   ALSO stop GNOME's own keyboard
#                                            popping up on touch (step 7; off by default)
#
# Default result ("GNOME standard", 2026-09-25): every third-party / extra
# keyboard is off, and GNOME's built-in keyboard appears when you tap a text
# field with your finger (the kiosk has no text fields, so it never shows there).
#
# Covers xvkbd, onboard, squeekboard, wvkbd and GNOME's built-in keyboard:
#   1. the "screen keyboard" accessibility setting (GNOME's keyboard AND squeekboard obey it)
#   2. login autostart entries (~/.config/autostart, /etc/xdg/autostart -> Hidden=true override)
#   3. compositor respawn lists (labwc / wayfire autostart) -- these relaunch a
#      keyboard straight after `pkill`, which is why killing it never stuck
#   4. systemd user services
#   5. the packages: uninstalled only when apt would remove nothing else
#   6. whatever is still running
#   7. GNOME (Ubuntu): its keyboard is built into gnome-shell and pops up on ANY
#      touch regardless of the setting in step 1 (keyboard.js: enabled =
#      setting || (touch_mode && lastDeviceIsTouchscreen)). A tiny extension,
#      no-touch-osk@barrett.com, makes that touch check return false.
#      Undo: gnome-extensions disable no-touch-osk@barrett.com
# Every file it edits gets a .bak-keyboard backup first. Reboot afterwards.
set -u
NAMES='xvkbd|onboard|squeekboard|wvkbd|matchbox-keyboard|florence|caribou'
# Running processes: match the program name only (".../onboard ..."), not any
# command line that merely mentions one of the names.
PROC_RE="(^|/)($NAMES)[^ /]*( |\$)"
PKGS="xvkbd onboard onboard-common onboard-data squeekboard wvkbd matchbox-keyboard florence"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1
BLOCK_GNOME=0; [ "${1:-}" = "--block-gnome-too" ] && BLOCK_GNOME=1

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
    # GNOME standard (default): switch ON -- the built-in keyboard then appears
    # whenever a text field is focused (touch auto-show alone didn't trigger on
    # the Pi). --block-gnome-too: switch OFF.
    want=true; [ $BLOCK_GNOME = 1 ] && want=false
    if [ "$cur" != "$want" ] && act "set screen-keyboard-enabled $want"; then
        gs set org.gnome.desktop.a11y.applications screen-keyboard-enabled $want && ok "set to $want"
    elif [ "$cur" = "$want" ]; then ok "already $want"; fi
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

# ── 7. GNOME: stop the built-in keyboard auto-showing on touch ──────────────
say "7. GNOME built-in keyboard (auto-shows on touch, ignoring step 1)"
EXT_UUID="no-touch-osk@barrett.com"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
if ! command -v gnome-shell >/dev/null 2>&1; then
    ok "not a GNOME desktop -- nothing to do"
else
    GMAJOR=$(gnome-shell --version 2>/dev/null | grep -o '[0-9]\+' | head -1)
    info "GNOME Shell ${GMAJOR:-?}"
    enabled_list=$(gs get org.gnome.shell enabled-extensions 2>/dev/null)
    if [ $BLOCK_GNOME = 0 ]; then
        # GNOME standard: make sure our blocker is OFF so the built-in keyboard works on touch
        if echo "$enabled_list" | grep -q "$EXT_UUID" && act "disable $EXT_UUID (use GNOME's built-in touch keyboard)"; then
            gnome-extensions disable "$EXT_UUID" 2>/dev/null
            new=$(echo "$enabled_list" | python3 -c "
import ast, sys
cur = sys.stdin.read().strip(); cur = cur.split(' ', 1)[1] if cur.startswith('@as') else cur
print(str([x for x in (ast.literal_eval(cur) if cur else []) if x != sys.argv[1]]))" "$EXT_UUID")
            gs set org.gnome.shell enabled-extensions "$new"
        fi
        ok "GNOME built-in keyboard pops up on touch in text fields (standard)"
    elif [ -e "$EXT_DIR/extension.js" ] && echo "$enabled_list" | grep -q "$EXT_UUID"; then
        ok "$EXT_UUID installed and enabled"
    elif act "install + enable $EXT_UUID"; then
        mkdir -p "$EXT_DIR"
        cat > "$EXT_DIR/metadata.json" <<EOF
{
  "uuid": "$EXT_UUID",
  "name": "No touch on-screen keyboard (Barrett pendulum)",
  "description": "Stops GNOME's on-screen keyboard popping up on touch. The accessibility 'Screen Keyboard' switch still works.",
  "shell-version": ["${GMAJOR:-46}"]
}
EOF
        if [ "${GMAJOR:-46}" -ge 45 ]; then
            cat > "$EXT_DIR/extension.js" <<'EOF'
// GNOME 45+: KeyboardManager._syncEnabled() enables the OSK when
// screen-keyboard-enabled OR (touch mode && last device is a touchscreen).
// Make the touchscreen check always false, so only the setting counts.
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

export default class NoTouchOsk extends Extension {
    enable() {
        const km = Main.keyboard;
        this._orig = km._lastDeviceIsTouchscreen;
        km._lastDeviceIsTouchscreen = () => false;
        km._syncEnabled();
    }

    disable() {
        const km = Main.keyboard;
        if (this._orig)
            km._lastDeviceIsTouchscreen = this._orig;
        this._orig = null;
        km._syncEnabled();
    }
}
EOF
        else
            cat > "$EXT_DIR/extension.js" <<'EOF'
// GNOME 42-44 (legacy extension format): same override as the 45+ version.
const Main = imports.ui.main;

class NoTouchOsk {
    enable() {
        const km = Main.keyboard;
        this._orig = km._lastDeviceIsTouchscreen;
        km._lastDeviceIsTouchscreen = () => false;
        km._syncEnabled();
    }

    disable() {
        const km = Main.keyboard;
        if (this._orig)
            km._lastDeviceIsTouchscreen = this._orig;
        this._orig = null;
        km._syncEnabled();
    }
}

function init() {
    return new NoTouchOsk();
}
EOF
        fi
        ok "installed $EXT_DIR"
        # Enable via the settings list (works before GNOME has rescanned
        # extensions; it loads at the next login).
        gs set org.gnome.shell disable-user-extensions false
        new_list=$(python3 - "$enabled_list" "$EXT_UUID" <<'EOF'
import ast, sys
cur, uuid = sys.argv[1].strip(), sys.argv[2]
cur = cur.split(' ', 1)[1] if cur.startswith('@as') else cur
items = ast.literal_eval(cur) if cur else []
if uuid not in items:
    items.append(uuid)
print(str(items))
EOF
)
        gs set org.gnome.shell enabled-extensions "$new_list" && ok "enabled (takes effect after the reboot)"
        gnome-extensions enable "$EXT_UUID" 2>/dev/null && ok "also enabled in the running session"
    fi
fi

# ── 8. third-party on-screen-keyboard GNOME extensions ──────────────────────
say "8. Third-party keyboard extensions (e.g. GJS OSK) -- kept OFF"
if command -v gnome-extensions >/dev/null 2>&1; then
    osk_ext=$(gnome-extensions list 2>/dev/null | grep -i -E 'osk|keyboard|kbd' | grep -v -x "$EXT_UUID")
    if [ -z "$osk_ext" ]; then ok "none installed"; fi
    for e in $osk_ext; do
        st=$(gnome-extensions info "$e" 2>/dev/null | awk '/Enabled:/{print $2}')
        if [ "$st" = "No" ] && ! gs get org.gnome.shell enabled-extensions | grep -q "$e"; then
            ok "$e already disabled"; continue
        fi
        info "found enabled: $e"
        if act "disable $e"; then
            gnome-extensions disable "$e" 2>/dev/null
            new=$(gs get org.gnome.shell enabled-extensions | python3 -c "
import ast, sys
cur = sys.stdin.read().strip(); cur = cur.split(' ', 1)[1] if cur.startswith('@as') else cur
items = [x for x in (ast.literal_eval(cur) if cur else []) if x != sys.argv[1]]
print(str(items))" "$e")
            gs set org.gnome.shell enabled-extensions "$new" && ok "disabled $e"
        fi
    done
else
    ok "gnome-extensions not available -- skipping"
fi

say "Done"
if [ $CHECK = 1 ]; then
    echo "    Nothing was changed (--check). Run without --check to apply."
else
    echo "    Reboot to finish:  sudo reboot"
    echo "    If a keyboard still appears after the reboot, run:  $0 --check"
    echo "    and send the output -- the 'parent' line shows what launched it."
fi
