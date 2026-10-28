#!/usr/bin/env bash
# Build Puck Utility for Windows 10 / 11 (x64): the PyInstaller one-dir app,
# a portable zip of it, and -- when Inno Setup 6 is installed -- the setup
# .exe (scripts/windows/puckutility.iss) with the optional PEAK PCAN and
# CANable driver components.
#
# Run from Git Bash (or any bash) on Windows, in the repo root venv
# (`python -m venv .` + `pip install -r requirements.txt` + `pip install ./p4core ./p4sysid .`).
#
# Options:
#   --no-bundle-peak   do not embed PeakOemDrv.exe (~150 MB); the installer
#                      downloads it at install time when the PEAK component
#                      is chosen
#   --refresh-drivers  re-download PeakOemDrv.exe instead of using the cache
#   --no-setup         stop after the zip (no Inno Setup step)
#
# Environment overrides:
#   PYTHON        python to build with (default: the repo venv's)
#   ISCC          Inno Setup compiler, one executable path (default: PATH,
#                 then Program Files); a wrapper script works for wine
#   SIGNTOOL      sign command for the setup + uninstaller, Inno style, e.g.
#                 'signtool sign /fd sha256 /a /tr http://timestamp.digicert.com /td sha256 $f'
#   FIRMWARE_DIR  puck firmware folder to ship (default: firmware/)
#
# Outputs (build/win/):
#   PuckUtilityApp-win32-vX.Y.Z/           the app folder
#   PuckUtilityApp-win32-vX.Y.Z.zip        portable copy (+ PeakOemDrv.exe)
#   PuckUtilityApp-win-vX.Y.Z-setup.exe    the installer
set -e
cd "$(dirname "$0")/.."

BUNDLE_PEAK=true
REFRESH_DRIVERS=false
MAKE_SETUP=true
while [ $# -gt 0 ]; do
    case "$1" in
        --no-bundle-peak)  BUNDLE_PEAK=false ;;
        --refresh-drivers) REFRESH_DRIVERS=true ;;
        --no-setup)        MAKE_SETUP=false ;;
        -h|--help)         sed -n '2,/^set -e/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# Resolve the venv's python explicitly so PyInstaller bundles the packages
# installed in the venv, not whatever bare `python` resolves to on PATH (on
# Windows often a system Python without wxPython/canopen).
if [ -n "${PYTHON:-}" ]; then
    read -ra PY <<<"$PYTHON"
elif [ -x "Scripts/python.exe" ]; then
    PY=(Scripts/python.exe)            # venv in the repo root
elif [ -x ".venv/Scripts/python.exe" ]; then
    PY=(.venv/Scripts/python.exe)
else
    echo "ERROR: could not find the venv python (Scripts/python.exe)." >&2
    echo "Create it with 'python -m venv .' and 'pip install -r requirements.txt'," >&2
    echo "or point PYTHON at one." >&2
    exit 1
fi
echo "Using python: ${PY[*]}"

FIRMWARE_DIR="${FIRMWARE_DIR:-firmware}"
if [ ! -d "$FIRMWARE_DIR" ]; then
    echo "ERROR: firmware folder '$FIRMWARE_DIR' not found -- the release ships the" >&2
    echo "puck firmware that system-config.ini names. Create it or set FIRMWARE_DIR." >&2
    exit 1
fi

VERSION=v$(grep -m1 '^__version__' puckutility/__init__.py | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
APP_VERSION="${VERSION#v}"
WIN="build/win"
OUTDIR="${WIN}/PuckUtilityApp-win32-${VERSION}"
ZIP="${WIN}/PuckUtilityApp-win32-${VERSION}.zip"

# Wipe leftovers from a half-completed previous run. Empty OUTDIR rather than
# removing it: on Windows the directory entry is sometimes held open by
# Explorer, a shell cd'd into it, or a stuck PuckUtilityApp.exe, even when
# the files inside are deletable.
if [ -d "${OUTDIR}" ]; then
    find "${OUTDIR}" -mindepth 1 -delete
fi
mkdir -p "${OUTDIR}"
rm -f "${ZIP}"

# --- PyInstaller: PuckUtilityApp.exe + PuckUtilityAppCLI.exe, one folder ----
"${PY[@]}" -m PyInstaller --noconfirm --clean \
    --distpath "${WIN}/pyinstaller/dist" --workpath "${WIN}/pyinstaller/work" \
    scripts/windows/PuckUtilityApp.spec
cp -r "${WIN}/pyinstaller/dist/PuckUtilityApp/." "${OUTDIR}/"

# Package data where p4core.paths.resource() looks beside the binary, and the
# technician's working folders (puckutility/paths.py APP_DIR).
mkdir -p "${OUTDIR}/puckutility" "${OUTDIR}/p4core" "${OUTDIR}/firmware"
cp -r puckutility/data "${OUTDIR}/puckutility/"
cp -r p4core/data "${OUTDIR}/p4core/"
cp -r config "${OUTDIR}/"
cp -r "${FIRMWARE_DIR}/." "${OUTDIR}/firmware/"
cp system-config.ini "${OUTDIR}/"
cp PuckUtilityAppGuide.pdf "${OUTDIR}/"

# --- PEAK driver setup (cached between builds) ------------------------------
PEAK_EXE="${WIN}/drivers/PeakOemDrv.exe"
PEAK_URL="https://web.barrett.com/support/Puck_ControlLibrary/PeakOemDrv.exe"
if [ "$REFRESH_DRIVERS" = true ] || [ ! -s "$PEAK_EXE" ]; then
    mkdir -p "${WIN}/drivers"
    echo "Downloading ${PEAK_URL}"
    curl -fL --retry 3 -o "${PEAK_EXE}.part" "$PEAK_URL"
    mv "${PEAK_EXE}.part" "$PEAK_EXE"
fi

# --- portable zip (the app folder + the PEAK setup beside it) ---------------
cp "$PEAK_EXE" "${OUTDIR}/"
# Python's zipfile, not tar: Git Bash's tar is GNU tar, which cannot write zips.
"${PY[@]}" -c 'import shutil, sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2], sys.argv[3])' \
    "${ZIP%.zip}" "${WIN}" "$(basename "$OUTDIR")"
rm -f "${OUTDIR}/PeakOemDrv.exe"     # the installer carries its own copy
echo "Built: ${ZIP}"

[ "$MAKE_SETUP" = true ] || exit 0

# --- Inno Setup -------------------------------------------------------------
if [ -z "${ISCC:-}" ]; then
    for c in "$(command -v iscc 2>/dev/null)" "$(command -v ISCC.exe 2>/dev/null)" \
             "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
             "/c/Program Files/Inno Setup 6/ISCC.exe"; do
        [ -n "$c" ] && [ -f "$c" ] && { ISCC="$c"; break; }
    done
fi
if [ -z "${ISCC:-}" ]; then
    echo "Inno Setup 6 not found: skipping the setup .exe (install it from" >&2
    echo "https://jrsoftware.org/isdl.php or set ISCC=...\\ISCC.exe)." >&2
    exit 0
fi

winpath() {   # a path ISCC (a Windows program) can open
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"
    elif command -v winepath >/dev/null 2>&1; then winepath -w "$1" 2>/dev/null
    else echo "$1"; fi
}

ISCC_ARGS=(
    "/DAppVersion=${APP_VERSION}"
    "/DSourceDir=$(winpath "$(pwd)/${OUTDIR}")"
    "/DOutputDir=$(winpath "$(pwd)/${WIN}")"
)
[ "$BUNDLE_PEAK" = true ] && ISCC_ARGS+=("/DPeakSetupFile=$(winpath "$(pwd)/${PEAK_EXE}")")
[ -n "${SIGNTOOL:-}" ] && ISCC_ARGS+=("/Spuck=${SIGNTOOL}" "/DSignTool=puck")
ISCC_ARGS+=("$(winpath "$(pwd)/scripts/windows/puckutility.iss")")

# Git Bash rewrites arguments that start with '/' into paths (/DAppVersion=...
# would become C:/Program Files/Git/DAppVersion=...): turn that off here.
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "$ISCC" "${ISCC_ARGS[@]}"
echo "Built: ${WIN}/PuckUtilityApp-win-${VERSION}-setup.exe"
