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
# Defaults to the repo-root .venv created by scripts/setup-venv.sh.
VENV_ROOT="${VENV_ROOT:-.venv}"

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
    # tee writes to both the terminal (stdout) and a per-user log file.
    # The display environment (GDK_BACKEND=x11, GSETTINGS_BACKEND=memory,
    # GTK_IM_MODULE, NO_AT_BRIDGE) is set by the app ITSELF at startup -- see the
    # top of puckutilityapp.py -- so it applies identically whether launched from
    # the dock, the terminal, or `./puckutilityapp.py` in dev. Nothing to export
    # here (and exporting GDK_BACKEND=wayland here would override the app's x11
    # default and re-break the layout on Wayland).
    cat > "${DEB_ROOT}/usr/bin/PuckUtilityApp" << 'WRAPPER'
#!/bin/bash
# Display env (GDK_BACKEND=x11, GSETTINGS_BACKEND=memory, GTK_IM_MODULE,
# NO_AT_BRIDGE) is set by the app at startup -- see the top of puckutilityapp.py.
# Keep this wrapper minimal so dev and packaged runs behave identically.
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

    # Install the icon into the hicolor theme at multiple sizes. GNOME Software /
    # App Center won't render an app icon below 64x64 and uses 128x128 for the
    # listing header, so a lone 48x48 shows the generic placeholder. The source
    # BarrettIcon.png is only 48px, so the larger sizes are upscaled here —
    # drop in a crisp 256x256 (or SVG) master for sharp results. pixmaps/ is the
    # legacy fallback path.
    HICOLOR="${DEB_ROOT}/usr/share/icons/hicolor"
    if ! "${PY:-python3}" - images/BarrettIcon.png "$HICOLOR" PuckUtilityApp <<'PYICON'
import os, sys
from PIL import Image
src, hicolor, name = sys.argv[1], sys.argv[2], sys.argv[3]
im = Image.open(src).convert("RGBA")
for sz in (48, 64, 128, 256):
    d = os.path.join(hicolor, f"{sz}x{sz}", "apps")
    os.makedirs(d, exist_ok=True)
    im.resize((sz, sz), Image.LANCZOS).save(os.path.join(d, f"{name}.png"))
PYICON
    then
        echo "WARNING: Pillow unavailable; installing single 48x48 icon only" >&2
        mkdir -p "${HICOLOR}/48x48/apps"
        cp images/BarrettIcon.png "${HICOLOR}/48x48/apps/PuckUtilityApp.png"
    fi
    cp images/BarrettIcon.png "${DEB_ROOT}/usr/share/pixmaps/PuckUtilityApp.png"

    # --- AppStream metadata -------------------------------------------------
    # Required for Ubuntu Software to show the icon, description, and version.
    RELEASE_DATE=$(date +%Y-%m-%d)
    cat > "${DEB_ROOT}/usr/share/metainfo/com.barrett.PuckUtilityApp.metainfo.xml" << METAINFO
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>com.barrett.PuckUtilityApp</id>
  <!-- metadata_license is MANDATORY: without it the whole component fails
       AppStream validation and GNOME Software/App Center silently drops it,
       leaving "Unknown publisher" / "License unknown" / no icon. -->
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>BSD-2-Clause</project_license>
  <name>Puck Utility</name>
  <summary>Configure and calibrate Barrett Technology Puck motors</summary>
  <description>
    <p>
      Puck Utility is a desktop tool for configuring and calibrating Barrett
      Technology P4 series motor controllers (Pucks) over a CAN bus.
    </p>
    <p>
      It supports firmware flashing, CANopen object dictionary configuration,
      encoder and cogging calibration, and CAN adapter setup.
    </p>
  </description>
  <icon type="stock">PuckUtilityApp</icon>
  <categories>
    <category>Utility</category>
  </categories>
  <url type="homepage">https://barrett.com</url>
  <!-- developer: new AppStream 1.0 form (Ubuntu 24.04+/26.04 App Center reads
       this for the publisher). developer_name is the deprecated form, kept for
       older AppStream on Ubuntu 20.04. -->
  <developer id="com.barrett">
    <name>Barrett Technology</name>
  </developer>
  <developer_name>Barrett Technology</developer_name>
  <launchable type="desktop-id">PuckUtilityApp.desktop</launchable>
  <!-- Links this component to the dpkg package so AppStream/App Center
       associate the package's install with this app's icon and publisher. -->
  <pkgname>${PKG_NAME}</pkgname>
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
Section: utils
Priority: optional
Installed-Size: ${INSTALLED_SIZE}
Maintainer: Barrett Technology <bn@barrett.com>
Homepage: https://barrett.com
Depends: can-utils, libgtk-3-0 | libgtk-3-0t64, libsdl2-2.0-0 | libsdl2-2.0-0t64, libnotify4 | libnotify4t64, libsm6 | libsm6t64, libxxf86vm1 | libxxf86vm1t64, libpcre2-32-0 | libpcre2-32-0t64, libsecret-1-0 | libsecret-1-0t64
Replaces: pucktunerapp, p4checkoutapp
Description: Barrett Technology Puck Utility
 GUI utility for configuring and calibrating Barrett Technology P4 series Puck
 motor controllers over CAN bus. Supports firmware flashing, CANopen object
 dictionary configuration, encoder calibration, and CAN adapter setup.
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
cat > /etc/NetworkManager/conf.d/99-puck-can.conf << 'EOF'
[keyfile]
unmanaged-devices=type:can
EOF

# Best-effort refresh: these no-op-fail in a chroot / container / image build
# where udev or systemd aren't running. The postinst must NOT abort on them
# (it runs under `set -e`), so tolerate failure -- the udev rules + service are
# already installed by dpkg itself; this just refreshes a live system.
udevadm control --reload-rules || true
udevadm trigger || true
systemctl daemon-reload || true

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor || true
fi

if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli refresh --force || true
fi

# --- Per-user desktop integration ---------------------------------------
# apt and PackageKit (GNOME Software) run maintainer scripts with a sanitized
# environment, so SUDO_USER is NOT available here: under the documented
# `apt install ./foo.deb` path it is empty and ${SUDO_USER:-$USER} resolves to
# "root", which silently skipped BOTH the Desktop shortcut (no /root/Desktop)
# and the dock pin (gated on != root). Detect every live graphical session
# directly via its D-Bus socket at /run/user/<uid>/bus and integrate for each
# real (uid >= 1000) user instead of trusting SUDO_USER.
integrate_for_user() {
    _uid="$1"
    _bus="/run/user/${_uid}/bus"
    [ -S "$_bus" ] || return 0
    _user=$(getent passwd "$_uid" | cut -d: -f1)
    _home=$(getent passwd "$_uid" | cut -d: -f6)
    [ -n "$_user" ] && [ -n "$_home" ] || return 0

    # File ops run as root (the postinst user); only the GUI D-Bus calls are
    # dropped to the session user via runuser. XDG_RUNTIME_DIR is passed
    # alongside the bus address so gio/gsettings resolve the user's session.
    _runas="runuser -u ${_user} -- env DBUS_SESSION_BUS_ADDRESS=unix:path=${_bus} XDG_RUNTIME_DIR=/run/user/${_uid}"

    # Deploy a trusted Desktop shortcut.
    if [ -d "${_home}/Desktop" ]; then
        _df="${_home}/Desktop/PuckUtilityApp.desktop"
        cp /usr/share/applications/PuckUtilityApp.desktop "$_df"
        chmod 755 "$_df"
        chown "${_user}:" "$_df"
        # Ubuntu 22+ Nautilus needs the executable bit (above) AND the
        # metadata::trusted xattr. Set it directly as root; fall back to gio
        # over the session bus on older GVFS-only systems.
        python3 -c "
import os, sys
try:
    os.setxattr(sys.argv[1], 'user.metadata::trusted', b'true')
except Exception as e:
    print('Warning: xattr trust failed:', e, file=sys.stderr)
    sys.exit(1)
" "$_df" 2>/dev/null || \
            $_runas gio set "$_df" metadata::trusted true 2>/dev/null || true
    fi

    # Pin to the GNOME / Ubuntu dock (org.gnome.shell favorite-apps). Ubuntu's
    # dock (ubuntu-dock / dash-to-dock) reads this same key.
    $_runas python3 -c '
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
if app_id not in apps:
    apps.append(app_id)
    gs("set", "org.gnome.shell", "favorite-apps", str(apps))
' 2>/dev/null || true
}

for _b in /run/user/[0-9]*/bus; do
    [ -S "$_b" ] || continue
    _u=$(basename "$(dirname "$_b")")
    [ "$_u" -ge 1000 ] 2>/dev/null || continue
    integrate_for_user "$_u" || true
done

exit 0
POSTINST
    chmod 755 "${DEB_ROOT}/DEBIAN/postinst"

    # --- DEBIAN/postrm ------------------------------------------------------
    cat > "${DEB_ROOT}/DEBIAN/postrm" << 'POSTRM'
#!/bin/bash
set -e
rm -f /etc/NetworkManager/conf.d/99-puck-can.conf
# Remove Desktop shortcut for all users who have one
for home in /home/*; do
    rm -f "${home}/Desktop/PuckUtilityApp.desktop"
done
# Unpin from the dock for every live session so no dead icon lingers (only on
# remove/purge, not upgrade -- "$1" is "upgrade" when dpkg is replacing us).
if [ "$1" != "upgrade" ]; then
    for _b in /run/user/[0-9]*/bus; do
        [ -S "$_b" ] || continue
        _u=$(basename "$(dirname "$_b")")
        [ "$_u" -ge 1000 ] 2>/dev/null || continue
        _user=$(getent passwd "$_u" | cut -d: -f1)
        [ -n "$_user" ] || continue
        runuser -u "$_user" -- env "DBUS_SESSION_BUS_ADDRESS=unix:path=${_b}" \
            "XDG_RUNTIME_DIR=/run/user/${_u}" python3 -c '
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
if app_id in apps:
    gs("set", "org.gnome.shell", "favorite-apps", str([a for a in apps if a != app_id]))
' 2>/dev/null || true
    done
fi
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
