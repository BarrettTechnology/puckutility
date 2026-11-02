#!/usr/bin/env bash
# Build the Windows app, zip and setup .exe on Linux, under wine.
#
# Sets up (once, cached) a wine prefix with Windows Python, a build venv and
# Inno Setup 6, then runs scripts/build-win32-installer.sh with PYTHON and
# ISCC pointing into it. scripts/build-win32-installer.sh runs this itself
# when started on Linux. Options are passed through to it (--no-bundle-peak,
# --refresh-drivers, --no-setup), as are FIRMWARE_DIR and SIGNTOOL.
#
# Needs: wine (tested with 9.0), curl; xvfb-run for the first-time installs
# when there is no display.
#
# Environment overrides:
#   WINE_BUILD_DIR   where the prefix and downloads live
#                    (default: ~/.cache/puckutility-wine; delete it to start over).
#                    It grows to a few GB: keep it on disk, not on a tmpfs.
#
# Wine 9.0 workarounds (all inside the private prefix only):
#   * wine's Python dies at startup when stdout is a file, so every wine call
#     has its output piped through cat;
#   * wine has no KERNEL32.CopyFile2, which Python 3.12's shutil calls: the
#     prefix's shutil.py is patched to skip it;
#   * numpy >= 2.2 needs ucrtbase.crealf, which wine lacks: if the pinned
#     numpy does not import, numpy 2.0.2 replaces it in the build venv;
#   * a venv on a Z: (host) path fails, so the venv lives on C:.
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'echo "ERROR: build-win32-wine.sh stopped at line $LINENO (exit $?)." >&2' ERR

PY_VERSION=3.12.10
INNO_VERSION=6.7.4
FALLBACK_NUMPY=2.0.2
BUILD_DIR="${WINE_BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/puckutility-wine}"
DL="$BUILD_DIR/downloads"

for t in wine curl; do
    command -v "$t" >/dev/null 2>&1 || { echo "ERROR: $t not found (sudo apt install $t)." >&2; exit 1; }
done
mkdir -p "$DL"

export WINEPREFIX="$BUILD_DIR/prefix"
export WINEDEBUG=-all
unset WINEDLLOVERRIDES
DRIVE_C="$WINEPREFIX/drive_c"
VENV_PY='C:\pubuild\Scripts\python.exe'

w() {   # run a wine command with stdout on a pipe, never a file or tty
    wine "$@" </dev/null 2>&1 | cat
}
w_installer() {   # an installer: give it a virtual display if there is none
    if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
        xvfb-run -a wine "$@" </dev/null 2>&1 | cat
    else
        w "$@"
    fi
}
install_once() {   # install_once FILE INSTALLER ARGS...: fail unless FILE appears
    local want="$1"; shift
    w_installer "$@" || true
    wineserver -w
    [ -f "$want" ] || { echo "ERROR: install failed, no $want (installer: $1)." >&2; exit 1; }
}
fetch() {   # fetch URL FILE: download into the cache once
    [ -s "$DL/$2" ] && return 0
    echo "Downloading $1"
    curl -fL --retry 3 -o "$DL/$2.part" "$1"
    mv "$DL/$2.part" "$DL/$2"
}

# The new prefix is created first, with no display so that the Mono / Gecko
# prompts cannot appear.
if [ ! -f "$WINEPREFIX/system.reg" ]; then
    echo "Creating the wine prefix $WINEPREFIX"
    env -u DISPLAY -u WAYLAND_DISPLAY wine wineboot -i </dev/null 2>&1 | cat || true
    wineserver -w
fi

# --- Windows Python ---------------------------------------------------------
if [ ! -f "$DRIVE_C/py312/python.exe" ]; then
    fetch "https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe" \
          "python-${PY_VERSION}-amd64.exe"
    echo "Installing Python ${PY_VERSION} into $WINEPREFIX"
    install_once "$DRIVE_C/py312/python.exe" "$DL/python-${PY_VERSION}-amd64.exe" /quiet InstallAllUsers=0 \
        'TargetDir=C:\py312' Include_launcher=0 Include_test=0 Include_doc=0 Shortcuts=0
fi
if grep -q '^    if hasattr(_winapi, "CopyFile2"):' "$DRIVE_C/py312/Lib/shutil.py"; then
    sed -i 's/^    if hasattr(_winapi, "CopyFile2"):/    if False:  # wine has no CopyFile2/' \
        "$DRIVE_C/py312/Lib/shutil.py"
    rm -rf "$DRIVE_C"/py312/Lib/__pycache__/shutil.*
fi

# --- build venv -------------------------------------------------------------
if [ ! -f "$DRIVE_C/pubuild/Scripts/python.exe" ]; then
    echo "Creating the build venv C:\\pubuild"
    w 'C:\py312\python.exe' -m venv 'C:\pubuild'
fi
REQ_SUM=$(sha256sum requirements.txt | cut -d' ' -f1)
if [ "$(cat "$BUILD_DIR/requirements.sha256" 2>/dev/null)" != "$REQ_SUM" ]; then
    echo "Installing requirements.txt into the build venv (takes a few minutes)"
    w "$VENV_PY" -m pip install -q --disable-pip-version-check -r requirements.txt
    if ! w "$VENV_PY" -c 'import numpy; print("import-ok")' | grep -q import-ok; then
        echo "numpy $(grep -m1 '^numpy==' requirements.txt | cut -d= -f3) does not load under this wine;" \
             "using numpy ${FALLBACK_NUMPY} in the build venv"
        w "$VENV_PY" -m pip install -q --disable-pip-version-check "numpy==${FALLBACK_NUMPY}"
    fi
    if ! w "$VENV_PY" -c 'import wx, numpy, matplotlib.pyplot, canopen, PyInstaller; print("import-ok")' \
            | grep -q import-ok; then
        echo "ERROR: the build venv does not import (delete $BUILD_DIR to start over)." >&2
        exit 1
    fi
    echo "$REQ_SUM" > "$BUILD_DIR/requirements.sha256"
fi
# The app itself, every time, so the build bundles the current checkout.
w "$VENV_PY" -m pip install -q --disable-pip-version-check --no-deps --force-reinstall ./p4core .

# --- Inno Setup -------------------------------------------------------------
if [ ! -f "$DRIVE_C/InnoSetup6/ISCC.exe" ]; then
    fetch "https://github.com/jrsoftware/issrc/releases/download/is-${INNO_VERSION//./_}/innosetup-${INNO_VERSION}.exe" \
          "innosetup-${INNO_VERSION}.exe"
    echo "Installing Inno Setup ${INNO_VERSION} into $WINEPREFIX"
    install_once "$DRIVE_C/InnoSetup6/ISCC.exe" "$DL/innosetup-${INNO_VERSION}.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART \
        /CURRENTUSER '/DIR=C:\InnoSetup6'
fi
cat > "$BUILD_DIR/iscc.sh" <<EOF
#!/usr/bin/env bash
set -o pipefail
export WINEPREFIX='$WINEPREFIX' WINEDEBUG=-all
wine 'C:\\InnoSetup6\\ISCC.exe' "\$@" </dev/null 2>&1 | cat
EOF
chmod +x "$BUILD_DIR/iscc.sh"

# --- the build --------------------------------------------------------------
PYTHON="wine $VENV_PY" ISCC="$BUILD_DIR/iscc.sh" \
    bash scripts/build-win32-installer.sh "$@" </dev/null 2>&1 | cat
