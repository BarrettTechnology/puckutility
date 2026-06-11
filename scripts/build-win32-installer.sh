#!/usr/bin/env bash
# Fail-fast on any error and resolve to the repo root regardless of where
# the script is invoked from (so it works from PowerShell, Git Bash, an
# IDE task runner, etc.).
set -e
cd "$(dirname "$0")/.."

# Resolve the venv's python explicitly so PyInstaller bundles the
# packages we installed via the venv, not whatever bare `python` happens
# to resolve to on PATH (which on Windows often points to a system
# Python that doesn't have our wxPython/canopen/etc.). Avoids the
# "no module named PyInstaller" friction users hit when they forget to
# Activate.ps1 first.
if [ -x "Scripts/python.exe" ]; then
    PY="Scripts/python.exe"          # Standard Windows venv
elif [ -x "bin/python" ]; then
    PY="bin/python"                  # Linux/macOS venv (rare on this script)
else
    echo "ERROR: could not find venv python in repo root." >&2
    echo "Create the venv with 'python -m venv .' and 'pip install -r requirements.txt'." >&2
    exit 1
fi
echo "Using venv python: $PY"

VERSION=$(grep -m1 'SetTitle' puckutilityapp.py | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')
OUTDIR="build/win/PuckUtilityApp-win32-${VERSION}"

# Wipe leftover files from a half-completed previous run so they can't
# pollute the new dist. We empty the OUTDIR's contents but DO NOT
# remove OUTDIR itself — on Windows the directory entry is sometimes
# held open by Explorer, a shell that `cd`'d into it, PyInstaller's
# bootloader, or a stuck PuckUtilityApp.exe, even when the individual
# files inside are deletable. `find -mindepth 1 -delete` walks
# depth-first and removes every entry under OUTDIR without touching
# OUTDIR itself.
if [ -d "${OUTDIR}" ]; then
    find "${OUTDIR}" -mindepth 1 -delete
fi
rm -f "build/win/PuckUtilityApp-win32-${VERSION}.zip"

# Windows notes (vs the Linux script):
#   - PyInstaller's --add-data uses ';' as the source/dest separator on
#     Windows (Linux uses ':').
#   - Windows venv layout is Lib/site-packages/ (no python3.X subdirectory).
#   - The CAN transport on Windows is PCAN, so hidden-import that interface
#     instead of socketcan.
#   - --icon is needed so the .exe carries the Win32 icon resource.
#
# We build TWO exes from the same source:
#   - PuckUtilityApp.exe    (windowed; double-click for the GUI, no console)
#   - PuckUtilityAppCLI.exe (console; for `--scan / --flash / --config /
#                            --calibrate / --system-config` from a terminal)
# Both share the bundled folders/files copied alongside, so there is no
# duplication outside the two .exe binaries themselves.
PI_COMMON=(
    --clean puckutilityapp.py --onefile
    --distpath "${OUTDIR}"
    --add-data="Lib/site-packages/canopen/;canopen/"
    --hiddenimport canopen
    --hiddenimport canopen.network
    --hiddenimport can
    --hiddenimport can.interfaces.pcan
    --icon=images/BarrettIcon.ico
)

# Windowed build (no console window flashes when launched from Explorer).
"$PY" -m PyInstaller "${PI_COMMON[@]}" --name PuckUtilityApp --noconsole

# Console build (stdout/stderr/input attach to the launching terminal).
"$PY" -m PyInstaller "${PI_COMMON[@]}" --name PuckUtilityAppCLI --console

cp -r images "${OUTDIR}"/
cp -r config "${OUTDIR}"/
cp puck4.eds "${OUTDIR}"/
cp system-config.ini "${OUTDIR}"/
cp flashloader.eds "${OUTDIR}"/
cp canopen_runner.py "${OUTDIR}"/
cp flashp4.py "${OUTDIR}"/
cp cli_ops.py "${OUTDIR}"/
cp PuckUtilityAppGuide.pdf "${OUTDIR}"/
cp -r firmware "${OUTDIR}"/
cp canable-candlelight-multiboard.bin "${OUTDIR}"/

# PCAN driver installer (downloaded fresh each build)
curl https://web.barrett.com/support/Puck_ControlLibrary/PeakOemDrv.exe -o PeakOemDrv.exe
mv PeakOemDrv.exe "${OUTDIR}"/

# Windows 10+ ships BSD tar; -a infers compression from the .zip suffix.
cd build/win
tar -a -cf "PuckUtilityApp-win32-${VERSION}.zip" "PuckUtilityApp-win32-${VERSION}/"
