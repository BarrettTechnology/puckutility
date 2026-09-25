#!/bin/bash
# Set up a Raspberry Pi (Touch Display 2, landscape) as the Barrett pendulum demo.
#
#   ./setup-pi.sh                   full setup (run as the desktop user, NOT with sudo;
#                                   it calls sudo for the system parts)
#   ./setup-pi.sh --check           diagnostics only -- changes nothing
#   ./setup-pi.sh --remove-autostart  stop the YES/NO prompt appearing at login
#   ./setup-pi.sh --desktop-icon      (re)create just the Barrett Pendulum desktop icon
#   ./setup-pi.sh --touch-display-only  just switch HDMI off (Touch Display only)
#
# What the full setup does (re-runnable; every step is idempotent):
#   1. apt: wxPython, venv, can-utils          5. screen never blanks / locks / sleeps
#   2. sandbox/.venv (canopen, python-can)    6. Barrett wallpaper
#   3. CAN: CANable (gs_usb) -> can0 @ 1 Mbit  7. desktop auto-login (so the prompt
#      via ../scripts/setup-socketcan.sh          appears after a power cycle)
#   4. login autostart: YES/NO boot prompt     8. app-menu entries: kiosk + engineering GUI
#                                              9. "Barrett Pendulum" icon on the desktop
#                                             10. HDMI off: the Touch Display is the only screen
#
# Works on Ubuntu (GNOME) and Raspberry Pi OS (labwc / wayfire / X11).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
USER_NAME="$(id -un)"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO=sudo; fi

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
bad()  { printf '    \033[31mXX\033[0m  %s\n' "$*"; }

. /etc/os-release 2>/dev/null
OS_ID="${ID:-unknown}"
IS_PIOS=0
if [ -e /usr/bin/raspi-config ] || grep -qi raspberry /etc/os-release 2>/dev/null; then IS_PIOS=1; fi

# Desktop flavour: gnome | labwc | wayfire | lxde | unknown
desktop_kind() {
    if command -v gnome-shell >/dev/null 2>&1; then echo gnome
    elif command -v labwc >/dev/null 2>&1; then echo labwc
    elif command -v wayfire >/dev/null 2>&1; then echo wayfire
    elif command -v lxsession >/dev/null 2>&1; then echo lxde
    else echo unknown; fi
}
DESKTOP="$(desktop_kind)"

# gsettings needs the user's session bus; over SSH there isn't one, so run it
# in a throwaway session (writes still land in ~/.config/dconf/user).
# GSETTINGS_BACKEND is unset in case this is run from a shell that has the
# apps' in-process "memory" backend exported.
gs() {
    if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
        env -u GSETTINGS_BACKEND gsettings "$@"
    else
        env -u GSETTINGS_BACKEND dbus-run-session -- gsettings "$@"
    fi
}

AUTOSTART="$HOME/.config/autostart/barrett-pendulum.desktop"
WALLPAPER_SRC="$HERE/assets/barrett-wallpaper.png"
WALLPAPER="$HOME/.local/share/backgrounds/barrett-wallpaper.png"

# ─────────────────────────────────────────────────────────────── check ──────
check() {
    say "System"
    echo "    OS: ${PRETTY_NAME:-?}   kernel: $(uname -r)   arch: $(uname -m)"
    echo "    desktop: $DESKTOP   session: ${XDG_SESSION_TYPE:-none (SSH?)}   user: $USER_NAME"

    say "Python"
    PY="$HERE/.venv/bin/python"
    if [ -x "$PY" ]; then ok "venv: $PY"; else bad "no venv at $HERE/.venv (run ./setup-pi.sh)"; PY=python3; fi
    "$PY" - <<'EOF' 2>&1 | sed 's/^/    /'
import sys; print("python", sys.version.split()[0])
for m in ("wx", "canopen", "can"):
    try:
        mod = __import__(m); print(f"OK  {m} {getattr(mod, '__version__', '')}")
    except Exception as e:
        print(f"XX  {m}: {e}")
EOF

    say "CAN"
    if lsmod | grep -q '^gs_usb'; then ok "gs_usb module loaded"; else warn "gs_usb not loaded (normal until a CANable is plugged in)"; fi
    if lsusb 2>/dev/null | grep -qi '1d50:606f'; then ok "CANable (candleLight) on USB"
    elif lsusb 2>/dev/null | grep -qi '0483:df11'; then bad "CANable is in DFU/bootloader mode -- reflash or replug"
    else bad "no CANable on USB (lsusb 1d50:606f)"; fi
    IFACES=$(ls /sys/class/net | grep '^can' || true)
    if [ -z "$IFACES" ]; then bad "no can* interface"; fi
    for i in $IFACES; do
        state=$(cat /sys/class/net/$i/operstate 2>/dev/null)
        info=$(ip -details link show "$i" 2>/dev/null | grep -o 'bitrate [0-9]*\|state [A-Z-]*\|dbitrate [0-9]*' | tr '\n' ' ')
        if [ "$state" = "up" ] || [ "$state" = "unknown" ]; then ok "$i $state  $info"; else bad "$i $state  $info"; fi
        systemctl is-active --quiet "can-up@$i.service" && ok "can-up@$i.service active" \
            || warn "can-up@$i.service not active (systemctl status can-up@$i)"
        if command -v candump >/dev/null 2>&1; then
            n=$(timeout 1 candump "$i" 2>/dev/null | wc -l)
            if [ "$n" -gt 0 ]; then ok "$n frames/s on $i (bus alive)"; else warn "no traffic on $i in 1 s (pucks off? nothing sending SYNC yet is normal)"; fi
        fi
    done
    for f in /etc/udev/rules.d/61-can-up.rules /etc/udev/rules.d/90-canable.rules /etc/systemd/system/can-up@.service; do
        [ -e "$f" ] && ok "$f" || bad "missing $f"
    done

    say "Kiosk"
    [ -e "$AUTOSTART" ] && ok "boot prompt autostart: $AUTOSTART" || warn "no boot prompt autostart"
    if [ -e /etc/gdm3/custom.conf ]; then
        grep -q "^AutomaticLogin=$USER_NAME" /etc/gdm3/custom.conf && ok "GDM auto-login as $USER_NAME" || warn "GDM auto-login not set"
    fi
    if [ "$DESKTOP" = gnome ]; then
        echo "    idle-delay: $(gs get org.gnome.desktop.session idle-delay 2>/dev/null)   lock: $(gs get org.gnome.desktop.screensaver lock-enabled 2>/dev/null)   sleep(ac): $(gs get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 2>/dev/null)"
        echo "    wallpaper: $(gs get org.gnome.desktop.background picture-uri 2>/dev/null)"
    fi
    [ -e "$HOME/.cache/barrett-pendulum/pendulum.log" ] && {
        echo "    last pendulum log (tail):"; tail -n 8 "$HOME/.cache/barrett-pendulum/pendulum.log" | sed 's/^/      /'; }
}

# ─────────────────────────────────────────────────────────────── steps ──────
install_packages() {
    say "1. System packages"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y python3-wxgtk4.0 python3-venv python3-pip can-utils \
        && ok "python3-wxgtk4.0, python3-venv, can-utils" \
        || { bad "apt install failed"; exit 1; }
}

setup_venv() {
    say "2. Python venv ($HERE/.venv)"
    # wxPython has no Linux/ARM wheels on PyPI; building it on a Pi takes hours.
    # Use the distro's python3-wxgtk4.0 through --system-site-packages instead.
    if [ ! -x "$HERE/.venv/bin/python" ]; then
        python3 -m venv --system-site-packages "$HERE/.venv" || { bad "venv failed"; exit 1; }
    fi
    CANOPEN=$(grep -i '^canopen==' "$REPO/requirements.txt" 2>/dev/null || echo canopen)
    PYCAN=$(grep -i '^python-can==' "$REPO/requirements.txt" 2>/dev/null || echo python-can)
    "$HERE/.venv/bin/pip" install -q --upgrade "$CANOPEN" "$PYCAN" || { bad "pip install failed"; exit 1; }
    "$HERE/.venv/bin/python" -c "import wx, canopen; print('    OK  wx', wx.version(), '| canopen', canopen.__version__)" \
        || { bad "wx/canopen import failed"; exit 1; }
}

setup_can() {
    say "3. CAN (CANable -> can0, 1 Mbit; tries CAN-FD first, falls back to classic)"
    # Same udev + can-up@.service + NetworkManager config as puckutility/pucktuner.
    sh "$REPO/scripts/setup-socketcan.sh" >/dev/null && ok "udev rules + can-up@.service installed" \
        || { bad "scripts/setup-socketcan.sh failed"; exit 1; }
    # Load the candleLight driver at boot even before the adapter enumerates.
    echo gs_usb | $SUDO tee /etc/modules-load.d/gs_usb.conf >/dev/null
    $SUDO modprobe gs_usb 2>/dev/null && ok "gs_usb loaded" || warn "gs_usb module not available in this kernel"
    $SUDO usermod -aG plugdev "$USER_NAME" 2>/dev/null || true
}

setup_autostart() {
    say "4. Boot prompt at login"
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Barrett Pendulum boot prompt
Comment=Asks whether to start the pendulum demo
Exec=$HERE/pendulum-launch.sh prompt
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=2
NoDisplay=true
EOF
    ok "$AUTOSTART"
}

setup_launchers() {
    say "8. App-menu entries (for staff after 'Exit to desktop')"
    APPS="$HOME/.local/share/applications"
    mkdir -p "$APPS"
    for mode in kiosk gui; do
        if [ $mode = kiosk ]; then name="Barrett Pendulum"; else name="Barrett Pendulum (engineering)"; fi
        cat > "$APPS/barrett-pendulum-$mode.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$name
Exec=$HERE/pendulum-launch.sh $mode
Icon=$HERE/assets/pendulum-icon.png
Terminal=false
Categories=Utility;
EOF
    done
    ok "$APPS/barrett-pendulum-{kiosk,gui}.desktop"
    setup_desktop_icon
}

setup_desktop_icon() {
    say "9. Desktop icon: Barrett Pendulum"
    DESK="$(xdg-user-dir DESKTOP 2>/dev/null)"
    if [ -z "$DESK" ] || [ "$DESK" = "$HOME" ]; then DESK="$HOME/Desktop"; fi   # xdg-user-dir falls back to $HOME
    mkdir -p "$DESK"
    ICON="$DESK/barrett-pendulum.desktop"
    cat > "$ICON" <<EOF
[Desktop Entry]
Type=Application
Name=Barrett Pendulum
Comment=Start the pendulum demo
Exec=$HERE/pendulum-launch.sh kiosk
Icon=$HERE/assets/pendulum-icon.png
Terminal=false
EOF
    chmod +x "$ICON"
    # GNOME (Ubuntu's desktop-icons extension) only launches "trusted" .desktop
    # files -- otherwise it shows them greyed out with "Allow Launching".
    if command -v gio >/dev/null 2>&1; then
        if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
            gio set "$ICON" metadata::trusted true 2>/dev/null
        else
            dbus-run-session -- gio set "$ICON" metadata::trusted true 2>/dev/null
        fi
        if [ $? -eq 0 ]; then ok "marked trusted (GNOME launches it without 'Allow Launching')"
        else warn "couldn't mark it trusted -- right-click the icon > Allow Launching"; fi
    fi
    # Raspberry Pi OS / pcmanfm: run launchers without the "Execute?" dialog.
    if command -v pcmanfm >/dev/null 2>&1; then
        LIBFM="$HOME/.config/libfm/libfm.conf"
        mkdir -p "$(dirname "$LIBFM")"
        [ -e "$LIBFM" ] || printf '[config]\n' > "$LIBFM"
        if grep -q '^quick_exec=' "$LIBFM"; then sed -i 's/^quick_exec=.*/quick_exec=1/' "$LIBFM"
        else sed -i '/^\[config\]/a quick_exec=1' "$LIBFM"; fi
        ok "pcmanfm: launch without the Execute prompt"
    fi
    ok "$ICON"
}

setup_no_blanking() {
    say "5. Screen: never blank, lock or sleep"
    case "$DESKTOP" in
    gnome)
        gs set org.gnome.desktop.session idle-delay 0
        gs set org.gnome.desktop.screensaver idle-activation-enabled false
        gs set org.gnome.desktop.screensaver lock-enabled false
        gs set org.gnome.desktop.lockdown disable-lock-screen true
        gs set org.gnome.settings-daemon.plugins.power idle-dim false
        gs set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
        gs set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
        # Customer-facing: no notification banners over the demo.
        gs set org.gnome.desktop.notifications show-banners false
        ok "GNOME: idle/lock/dim/sleep off, notification banners off"
        ;;
    *)
        if command -v raspi-config >/dev/null 2>&1; then
            $SUDO raspi-config nonint do_blanking 1 && ok "raspi-config: screen blanking disabled"
        fi
        # labwc / wayfire idle blanking is swayidle started from the autostart file.
        for f in "$HOME/.config/labwc/autostart" /etc/xdg/labwc/autostart; do
            if [ -e "$f" ] && grep -q '^[^#]*swayidle' "$f"; then
                if [ "$f" = "$HOME/.config/labwc/autostart" ]; then
                    sed -i 's/^\([^#]*swayidle\)/# disabled by setup-pi.sh: \1/' "$f"
                else
                    mkdir -p "$HOME/.config/labwc"
                    [ -e "$HOME/.config/labwc/autostart" ] || grep -v 'swayidle' "$f" > "$HOME/.config/labwc/autostart"
                fi
                ok "labwc: swayidle disabled ($f)"
            fi
        done
        # X11 sessions: DPMS + screensaver off at login.
        if [ "$DESKTOP" = lxde ] || [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
            cat > "$HOME/.config/autostart/barrett-no-blank.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Disable screen blanking
Exec=sh -c "xset s off; xset s noblank; xset -dpms"
NoDisplay=true
EOF
            ok "X11: xset s off / -dpms at login"
        fi
        ;;
    esac
    # Console blanking (kernel), for completeness.
    for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
        if [ -e "$c" ]; then
            if ! grep -q 'consoleblank=0' "$c"; then
                $SUDO cp "$c" "$c.bak-pendulum"
                $SUDO sed -i '1 s/$/ consoleblank=0/' "$c" && ok "consoleblank=0 in $c (backup: $c.bak-pendulum)"
            fi
            break
        fi
    done
}

setup_wallpaper() {
    say "6. Barrett wallpaper"
    mkdir -p "$(dirname "$WALLPAPER")"
    cp "$WALLPAPER_SRC" "$WALLPAPER"
    case "$DESKTOP" in
    gnome)
        gs set org.gnome.desktop.background picture-uri "file://$WALLPAPER"
        gs set org.gnome.desktop.background picture-uri-dark "file://$WALLPAPER"
        gs set org.gnome.desktop.background picture-options 'zoom'
        gs set org.gnome.desktop.background primary-color '#ffffff'
        gs set org.gnome.desktop.screensaver picture-uri "file://$WALLPAPER"
        ok "GNOME desktop + lock-screen background"
        ;;
    *)
        # Raspberry Pi OS: pcmanfm draws the desktop.  Live session -> set it
        # directly; otherwise edit every pcmanfm profile's config.
        if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v pcmanfm >/dev/null 2>&1 \
           && pcmanfm --set-wallpaper "$WALLPAPER" --wallpaper-mode=fit 2>/dev/null; then
            ok "pcmanfm wallpaper set"
        else
            found=0
            for conf in "$HOME"/.config/pcmanfm/*/desktop-items-*.conf; do
                [ -e "$conf" ] || continue
                found=1
                sed -i "s|^wallpaper=.*|wallpaper=$WALLPAPER|; s|^wallpaper_mode=.*|wallpaper_mode=fit|; s|^desktop_bg=.*|desktop_bg=#ffffff|" "$conf"
                ok "$conf"
            done
            [ $found = 1 ] || warn "no pcmanfm config yet -- log into the desktop once and re-run, or set $WALLPAPER by hand"
        fi
        ;;
    esac
}

setup_touch_display_only() {
    say "10. Display: left on auto-detect (no changes)"
    # Both attempts to pin the display to the Touch Display broke touch on this
    # Pi (explicit ili9881 overlay + display_auto_detect=0, and/or
    # video=HDMI-A-n:d on the kernel command line), so this step no longer
    # changes anything. If a power-on misses the panel (black touch screen),
    # power-cycle once.
    ok "nothing to do"
}

setup_autologin() {
    say "7. Desktop auto-login as $USER_NAME"
    if [ -e /etc/gdm3/custom.conf ]; then
        $SUDO cp -n /etc/gdm3/custom.conf /etc/gdm3/custom.conf.bak-pendulum
        $SUDO python3 - "$USER_NAME" <<'EOF'
import re, sys
p, user = '/etc/gdm3/custom.conf', sys.argv[1]
s = open(p).read()
if '[daemon]' not in s:
    s = '[daemon]\n' + s
def put(s, key, val):
    pat = re.compile(rf'^#?\s*{key}\s*=.*$', re.M)
    if pat.search(s):
        return pat.sub(f'{key}={val}', s, count=1)
    return s.replace('[daemon]', f'[daemon]\n{key}={val}', 1)
s = put(s, 'AutomaticLoginEnable', 'true')
s = put(s, 'AutomaticLogin', user)
open(p, 'w').write(s)
EOF
        ok "GDM (/etc/gdm3/custom.conf, backup .bak-pendulum)"
    elif command -v raspi-config >/dev/null 2>&1; then
        $SUDO raspi-config nonint do_boot_behaviour B4 && ok "raspi-config: desktop auto-login"
    elif [ -d /etc/lightdm ]; then
        $SUDO mkdir -p /etc/lightdm/lightdm.conf.d
        printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$USER_NAME" \
            | $SUDO tee /etc/lightdm/lightdm.conf.d/50-barrett-autologin.conf >/dev/null
        ok "LightDM auto-login"
    else
        warn "unknown display manager -- enable auto-login for $USER_NAME by hand"
    fi
}

# ─────────────────────────────────────────────────────────────── main ───────
case "${1:-}" in
    --check) check; exit 0 ;;
    --remove-autostart) rm -f "$AUTOSTART"; echo "Removed $AUTOSTART"; exit 0 ;;
    --desktop-icon) setup_desktop_icon; exit 0 ;;
    --touch-display-only) setup_touch_display_only; exit 0 ;;
    "") ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as the desktop user (it uses sudo where needed), not as root." >&2
    exit 1
fi
echo "Setting up the Barrett pendulum kiosk on ${PRETTY_NAME:-this system} (desktop: $DESKTOP)"
install_packages
setup_venv
setup_can
setup_autostart
setup_no_blanking
setup_wallpaper
setup_autologin
setup_launchers
setup_touch_display_only

say "Done"
echo "    Reboot to test the full boot flow:  sudo reboot"
echo "    Diagnostics any time:               $HERE/setup-pi.sh --check"
echo "    Run now without rebooting:          $HERE/pendulum-launch.sh prompt"
