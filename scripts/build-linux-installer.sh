#!/usr/bin/env bash
# Fail-fast on any error and resolve to the repo root regardless of where
# the script is invoked from.
set -e
cd "$(dirname "$0")/.."

FORMAT="pyinstaller"
USE_DOCKER=false
REBUILD_DOCKER=false
CLEAN_DOCKER=false
CLEAN_PYINSTALLER=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --deb)               FORMAT="deb" ;;
        --pyinstaller)       FORMAT="pyinstaller" ;;
        --docker)            USE_DOCKER=true ;;
        --rebuild-docker)    USE_DOCKER=true; REBUILD_DOCKER=true ;;
        --clean-docker)      USE_DOCKER=true; REBUILD_DOCKER=true; CLEAN_DOCKER=true ;;
        --clean-pyinstaller) CLEAN_PYINSTALLER=true ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# --docker: re-invoke this script inside a pinned Ubuntu 20.04 container for
# reproducible glibc/GTK linkage. Ensures the binary runs on Ubuntu >= 20.04
# regardless of the dev machine's OS.
#
# The builder image (puckutility-builder) pre-bakes all pip dependencies so
# wxPython only compiles once. Use --rebuild-docker to force a fresh image
# (e.g. after changing requirements.txt).
if [ "$USE_DOCKER" = true ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: docker not found. Install Docker and try again." >&2
        exit 1
    fi

    IMAGE="puckutility-builder:latest"

    if [ "$REBUILD_DOCKER" = true ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "Building Docker builder image..."
        DOCKER_BUILD_FLAGS=()
        [ "$CLEAN_DOCKER" = true ] && DOCKER_BUILD_FLAGS+=(--no-cache)
        docker build "${DOCKER_BUILD_FLAGS[@]}" -t "$IMAGE" -f scripts/Dockerfile.builder .
    fi

    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -v "$(pwd):/build" \
        -w /build \
        "$IMAGE" \
        bash -c "VENV_ROOT=/opt/puckbuild bash scripts/build-linux-installer.sh --${FORMAT}"
    exit 0
fi

# VENV_ROOT is overridden to /opt/puckbuild when invoked from --docker above.
# Defaults to the repo root where the dev venv lives.
VENV_ROOT="${VENV_ROOT:-.}"

if [ -x "${VENV_ROOT}/bin/python" ]; then
    PY="${VENV_ROOT}/bin/python"
elif [ -x "Scripts/python.exe" ]; then
    PY="Scripts/python.exe"
else
    echo "WARNING: no venv python found; falling back to system pyinstaller" >&2
    PY=""
fi

VERSION=$(grep -m1 'SetTitle' puckutilityapp.py | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')
TITLE_LINE=$(grep -m1 'SetTitle' puckutilityapp.py)
if echo "$TITLE_LINE" | grep -qiE '\bDEV\b'; then
    VERSION="${VERSION}-dev"
fi

# Resolve canopen source path — handles python3.X version variations and
# external venv roots set by --docker.
CANOPEN_SRC=$(ls -d "${VENV_ROOT}/lib/python3"*/site-packages/canopen/ 2>/dev/null | head -1)
if [ -z "$CANOPEN_SRC" ]; then
    echo "ERROR: canopen package not found in venv at ${VENV_ROOT}" >&2
    exit 1
fi

# Build PyInstaller binary into a staging directory common to both formats.
BINARY_STAGE="build/lin/.pyinstaller-stage"
rm -rf "${BINARY_STAGE}"
mkdir -p "${BINARY_STAGE}"

PYINSTALLER_ARGS=(
    puckutilityapp.py
    --name PuckUtilityApp
    --onefile
    --distpath "${BINARY_STAGE}"
    --add-data="${CANOPEN_SRC}:canopen/"
    --hiddenimport canopen
    --hiddenimport canopen.network
    --hiddenimport canopen.objectdictionary
    --hiddenimport canopen.node
    --hiddenimport canopen.node.base
    --hiddenimport canopen.node.local
    --hiddenimport canopen.node.remote
    --hiddenimport canopen.sdo
    --hiddenimport canopen.pdo
    --hiddenimport canopen.nmt
    --hiddenimport canopen.emcy
    --hiddenimport canopen.sync
    --hiddenimport canopen.variable
    --hiddenimport can
    --hiddenimport can.interfaces
    --hiddenimport can.interfaces.socketcan
    --hiddenimport can.interfaces.socketcan.socketcan
    --hiddenimport can.util
    --hiddenimport can.bit_timing
    --hiddenimport can.typechecking
    --hiddenimport can_backend
    --runtime-hook=scripts/set_x11.py
)

# Bundle GTK pixbuf loaders so PNG/JPEG rendering works on any Ubuntu version.
# gdk-pixbuf-query-loaders generates a cache with absolute paths; we replace
# them with a placeholder that the runtime hook swaps for sys._MEIPASS at
# launch time. This avoids depending on the target system's loader paths.
if [ "$(uname)" = "Linux" ]; then
    LOADERS_DIR=$(find /usr/lib -name 'libpixbufloader-png.so' 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    [ -z "$LOADERS_DIR" ] && LOADERS_DIR=$(find /usr/lib -path "*/gdk-pixbuf-2.0/*/loaders" -type d 2>/dev/null | head -1)
    GDK_QUERY=$(command -v gdk-pixbuf-query-loaders 2>/dev/null || \
        find /usr/lib -name 'gdk-pixbuf-query-loaders' 2>/dev/null | head -1)
    if [ -n "$LOADERS_DIR" ] && [ -n "$GDK_QUERY" ]; then
        CACHE_TEMPLATE="build/lin/pixbuf-loaders.cache"
        mkdir -p "$(dirname "$CACHE_TEMPLATE")"
        GDK_PIXBUF_MODULEDIR="${LOADERS_DIR}" "$GDK_QUERY" 2>/dev/null \
            | sed "s|${LOADERS_DIR}|MEIPASS_PLACEHOLDER/gdk-pixbuf-loaders|g" \
            > "${CACHE_TEMPLATE}"
        PYINSTALLER_ARGS+=(
            --add-data="${LOADERS_DIR}:gdk-pixbuf-loaders/"
            --add-data="${CACHE_TEMPLATE}:."
        )
        echo "Bundling pixbuf loaders from ${LOADERS_DIR}"
    else
        echo "WARNING: gdk-pixbuf loaders not found — PNG/JPEG may fail on target" >&2
    fi
fi

if [ "$CLEAN_PYINSTALLER" = true ]; then
    PYINSTALLER_ARGS=(--clean "${PYINSTALLER_ARGS[@]}")
fi

if [ -n "$PY" ]; then
    "$PY" -m PyInstaller "${PYINSTALLER_ARGS[@]}"
else
    pyinstaller "${PYINSTALLER_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
if [ "$FORMAT" = "pyinstaller" ]; then
# ---------------------------------------------------------------------------

    OUTDIR="build/lin/PuckUtilityApp-lin-${VERSION}"

    if [ -d "${OUTDIR}" ]; then
        find "${OUTDIR}" -mindepth 1 -delete
    fi
    rm -f "build/PuckUtilityApp-lin-${VERSION}.zip"

    cp "${BINARY_STAGE}/PuckUtilityApp" "${OUTDIR}/"
    cp -r images/          "${OUTDIR}/"
    cp -r config/          "${OUTDIR}/"
    cp puck4.eds           "${OUTDIR}/"
    cp system-config.ini   "${OUTDIR}/"
    cp flashloader.eds     "${OUTDIR}/"
    cp scripts/reset_can.sh        "${OUTDIR}/"
    cp scripts/60-can.rules        "${OUTDIR}/"
    cp scripts/61-can-up.rules     "${OUTDIR}/"
    cp canopen_runner.py   "${OUTDIR}/"
    cp flashp4.py          "${OUTDIR}/"
    cp cli_ops.py          "${OUTDIR}/"
    cp scripts/setup-socketcan.sh  "${OUTDIR}/"
    cp scripts/install-ubuntu.sh   "${OUTDIR}/"
    cp PuckUtilityApp.desktop      "${OUTDIR}/"
    cp PuckUtilityAppGuide.pdf     "${OUTDIR}/"
    cp -r firmware/        "${OUTDIR}/"
    cp canable-candlelight-multiboard.bin "${OUTDIR}/"

    cd build/lin
    zip -r "../PuckUtilityApp-lin-${VERSION}.zip" "PuckUtilityApp-lin-${VERSION}"
    echo "Built: build/PuckUtilityApp-lin-${VERSION}.zip"

# ---------------------------------------------------------------------------
elif [ "$FORMAT" = "deb" ]; then
# ---------------------------------------------------------------------------

    # dpkg version strings may not start with 'v'
    DEB_VERSION="${VERSION#v}"
    PKG_NAME="puckutilityapp"
    DEB_ROOT="build/deb/${PKG_NAME}_${DEB_VERSION}_amd64"

    rm -rf "${DEB_ROOT}"

    APP_DIR="${DEB_ROOT}/usr/share/puckutilityapp"
    mkdir -p "${APP_DIR}"
    mkdir -p "${DEB_ROOT}/usr/bin"
    mkdir -p "${DEB_ROOT}/usr/share/applications"
    mkdir -p "${DEB_ROOT}/usr/share/icons/hicolor/48x48/apps"
    mkdir -p "${DEB_ROOT}/usr/share/metainfo"
    mkdir -p "${DEB_ROOT}/usr/share/pixmaps"
    mkdir -p "${DEB_ROOT}/etc/udev/rules.d"
    mkdir -p "${DEB_ROOT}/etc/systemd/system"
    mkdir -p "${DEB_ROOT}/DEBIAN"

    # --- App binary and data files ------------------------------------------
    cp "${BINARY_STAGE}/PuckUtilityApp" "${APP_DIR}/"
    chmod 755 "${APP_DIR}/PuckUtilityApp"
    cp -r images/        "${APP_DIR}/"
    cp -r config/        "${APP_DIR}/"
    cp puck4.eds         "${APP_DIR}/"
    cp system-config.ini "${APP_DIR}/"
    cp flashloader.eds   "${APP_DIR}/"
    cp scripts/reset_can.sh "${APP_DIR}/"
    cp canopen_runner.py "${APP_DIR}/"
    cp can_backend.py   "${APP_DIR}/"
    cp flashp4.py        "${APP_DIR}/"
    cp cli_ops.py        "${APP_DIR}/"
    cp -r firmware/      "${APP_DIR}/"
    cp canable-candlelight-multiboard.bin "${APP_DIR}/"

    # reset_can.sh at /usr/bin so the old udev rule path and manual invocation
    # both work (script uses sudo internally for manual use).
    cp scripts/reset_can.sh "${DEB_ROOT}/usr/bin/reset_can.sh"
    chmod 755 "${DEB_ROOT}/usr/bin/reset_can.sh"

    # Wrapper launched by both the desktop entry and the terminal.
    # - tee writes to both the terminal (stdout) and a per-user log file.
    # - GDK_BACKEND=wayland,x11: try Wayland first so GNOME Shell's
    #   xdg-activation protocol is used and the window gets focus when
    #   launched from the dock on Ubuntu 22+.  Falls back to X11/XWayland
    #   on pure X11 sessions or when Wayland is unavailable.
    cat > "${DEB_ROOT}/usr/bin/PuckUtilityApp" << 'WRAPPER'
#!/bin/bash
# Prefer the Wayland GTK backend so GNOME Shell can activate the window via
# the xdg-activation protocol when the app is launched from the dock.
# Falls back to X11 automatically on X11-only sessions.
export GDK_BACKEND=wayland,x11

# Use an in-memory GSettings backend so GTK doesn't read the system dconf
# schemas. On Ubuntu 26 the GNOME Settings Daemon changed the xsettings
# schema (removed the 'antialiasing' key) which causes a fatal GLib-GIO-ERROR
# when the binary built on Ubuntu 20.04 tries to read it from the dock.
export GSETTINGS_BACKEND=memory

# Prevent GTK from loading the ibus IM module; the system ibus library on
# Ubuntu 26 is built against a newer GLib and emits dozens of
# 'undefined symbol: g_task_set_static_name' warnings on every keypress.
export GTK_IM_MODULE=gtk-im-context-simple

# Suppress the accessibility bridge warning (atk-bridge / at-spi2).
export NO_AT_BRIDGE=1

cd /usr/share/puckutilityapp
LOG="/tmp/puckutilityapp-$(id -un).log"
echo "=== launch $(date --iso-8601=seconds) ===" >> "$LOG"
/usr/share/puckutilityapp/PuckUtilityApp "$@" 2>&1 | tee -a "$LOG"
WRAPPER
    chmod 755 "${DEB_ROOT}/usr/bin/PuckUtilityApp"

    # --- Desktop integration ------------------------------------------------
    cat > "${DEB_ROOT}/usr/share/applications/PuckUtilityApp.desktop" << 'DESKTOP'
[Desktop Entry]
Type=Application
Terminal=false
Name=Puck Utility
Exec=/usr/bin/PuckUtilityApp
Icon=PuckUtilityApp
Categories=Utility;
StartupNotify=true
StartupWMClass=PuckUtilityApp
DESKTOP

    # Install icon into the hicolor theme (reliable GNOME lookup) and pixmaps (fallback)
    cp images/BarrettIcon.png "${DEB_ROOT}/usr/share/icons/hicolor/48x48/apps/PuckUtilityApp.png"
    cp images/BarrettIcon.png "${DEB_ROOT}/usr/share/pixmaps/PuckUtilityApp.png"

    # --- AppStream metadata -------------------------------------------------
    # Required for Ubuntu Software to show the icon, description, and version.
    RELEASE_DATE=$(date +%Y-%m-%d)
    cat > "${DEB_ROOT}/usr/share/metainfo/com.barrett.PuckUtilityApp.metainfo.xml" << METAINFO
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>com.barrett.PuckUtilityApp</id>
  <name>Puck Utility</name>
  <summary>Configure and calibrate Barrett Technology Puck motors</summary>
  <description>
    <p>
      GUI utility for configuring and calibrating Barrett Technology P4 series
      motor controllers over CAN bus. Supports firmware flashing, CANopen object
      dictionary configuration, encoder calibration, and CAN adapter setup.
    </p>
  </description>
  <icon type="stock">PuckUtilityApp</icon>
  <categories>
    <category>Utility</category>
  </categories>
  <url type="homepage">https://barrett.com</url>
  <developer_name>Barrett Technology</developer_name>
  <launchable type="desktop-id">PuckUtilityApp.desktop</launchable>
  <releases>
    <release version="${DEB_VERSION}" date="${RELEASE_DATE}"/>
  </releases>
  <content_rating type="oars-1.1"/>
</component>
METAINFO

    # --- udev rules ---------------------------------------------------------
    cp scripts/60-can.rules        "${DEB_ROOT}/etc/udev/rules.d/"
    cp scripts/61-can-up.rules     "${DEB_ROOT}/etc/udev/rules.d/"
    [ -f scripts/90-canable.rules ] && \
        cp scripts/90-canable.rules "${DEB_ROOT}/etc/udev/rules.d/"
    cp scripts/can-up@.service     "${DEB_ROOT}/etc/systemd/system/"

    # --- DEBIAN/control -----------------------------------------------------
    INSTALLED_SIZE=$(du -sk "${DEB_ROOT}" | cut -f1)
    cat > "${DEB_ROOT}/DEBIAN/control" << CONTROL
Package: ${PKG_NAME}
Version: ${DEB_VERSION}
Architecture: amd64
Installed-Size: ${INSTALLED_SIZE}
Maintainer: Barrett Technology <bn@barrett.com>
Depends: can-utils
Description: Barrett Technology Puck Utility
 GUI utility for configuring and calibrating Barrett Technology Puck motors.
CONTROL

    # --- DEBIAN/postinst ----------------------------------------------------
    # Reload udev so the CAN rules take effect immediately and configure the
    # can0 network interface if it isn't already present.
    cat > "${DEB_ROOT}/DEBIAN/postinst" << 'POSTINST'
#!/bin/bash
set -e

# Tell NetworkManager to leave SocketCAN interfaces alone so it doesn't
# race against can-up@.service or bring can0 back down after we bring it up.
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-puckutility-can.conf << 'EOF'
[keyfile]
unmanaged-devices=type:can
EOF

udevadm control --reload-rules
udevadm trigger
systemctl daemon-reload

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor
fi

if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli refresh --force || true
fi

DESKTOP_USER="${SUDO_USER:-$USER}"
DESKTOP_HOME=$(getent passwd "$DESKTOP_USER" 2>/dev/null | cut -d: -f6)
DESKTOP_UID=$(id -u "$DESKTOP_USER" 2>/dev/null || true)

# Deploy a Desktop shortcut for the installing user.
if [ -n "$DESKTOP_HOME" ] && [ -d "${DESKTOP_HOME}/Desktop" ]; then
    DESKTOP_FILE="${DESKTOP_HOME}/Desktop/PuckUtilityApp.desktop"
    cp /usr/share/applications/PuckUtilityApp.desktop "$DESKTOP_FILE"
    chmod 755 "$DESKTOP_FILE"
    chown "$DESKTOP_USER": "$DESKTOP_FILE"
    # Mark as trusted so Nautilus shows "Puck Utility" and allows launching.
    # Ubuntu 22+ Nautilus requires both the executable bit (done above) AND
    # the metadata::trusted xattr set to "true".
    # Set the xattr directly as root — no GVFS daemon or D-Bus needed.
    python3 -c "
import os, sys
try:
    os.setxattr(sys.argv[1], 'user.metadata::trusted', b'true')
except Exception as e:
    print('Warning: xattr trust failed:', e, file=sys.stderr)
    sys.exit(1)
" "$DESKTOP_FILE" 2>/dev/null || {
        # Fallback: gio via user's session bus (Ubuntu 20.04 / GVFS daemon path)
        _BUS="/run/user/${DESKTOP_UID}/bus"
        if [ -n "$DESKTOP_UID" ] && [ -S "$_BUS" ]; then
            runuser -u "$DESKTOP_USER" -- env "DBUS_SESSION_BUS_ADDRESS=unix:path=${_BUS}" \
                gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
        fi
    }
fi

# Refresh the GNOME Shell dock/dash entry so the updated .desktop file is
# picked up immediately without requiring a manual unpin/re-pin.
# Strategy: remove the old entry then re-append it so GNOME Shell sees a
# GSettings change and re-reads the file from /usr/share/applications/.
# Requires the user's D-Bus session bus to be reachable (i.e. they are
# logged in with a live GNOME session at install time).
if [ -n "$DESKTOP_UID" ] && [ "$DESKTOP_USER" != "root" ]; then
    _BUS="/run/user/${DESKTOP_UID}/bus"
    if [ -S "$_BUS" ]; then
        runuser -u "$DESKTOP_USER" -- env "DBUS_SESSION_BUS_ADDRESS=unix:path=${_BUS}" \
            python3 -c '
import subprocess, ast, os, sys
app_id = "PuckUtilityApp.desktop"
env = os.environ.copy()
def gs(*a):
    return subprocess.run(["gsettings", *a], capture_output=True, text=True, env=env)
r = gs("get", "org.gnome.shell", "favorite-apps")
if r.returncode != 0:
    sys.exit(0)
try:
    apps = ast.literal_eval(r.stdout.strip())
    assert isinstance(apps, list)
except Exception:
    sys.exit(0)
apps = [a for a in apps if a != app_id]
apps.append(app_id)
gs("set", "org.gnome.shell", "favorite-apps", str(apps))
' 2>/dev/null || true
    fi
fi

exit 0
POSTINST
    chmod 755 "${DEB_ROOT}/DEBIAN/postinst"

    # --- DEBIAN/postrm ------------------------------------------------------
    cat > "${DEB_ROOT}/DEBIAN/postrm" << 'POSTRM'
#!/bin/bash
set -e
rm -f /etc/NetworkManager/conf.d/99-puckutility-can.conf
# Remove Desktop shortcut for all users who have one
for home in /home/*; do
    rm -f "${home}/Desktop/PuckUtilityApp.desktop"
done
systemctl daemon-reload || true
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor || true
fi
exit 0
POSTRM
    chmod 755 "${DEB_ROOT}/DEBIAN/postrm"

    dpkg-deb --build --root-owner-group "${DEB_ROOT}"
    echo "Built: ${DEB_ROOT}.deb"

fi
