#!/usr/bin/env bash
# Fail-fast on any error and resolve to the repo root regardless of where
# the script is invoked from.
set -e
cd "$(dirname "$0")/.."

# Resolve the venv's python explicitly so the bundled packages match the
# venv we actually installed dependencies into. Falls back to system
# `pyinstaller` if no venv is present (developer convenience).
if [ -x "bin/python" ]; then
    PY="bin/python"
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
OUTDIR="build/lin/PuckUtilityApp-lin-${VERSION}"

# Wipe leftover files from a half-completed previous run. Empty the
# contents of OUTDIR without removing OUTDIR itself — keeps the script
# behaviour consistent with the Windows variant where parent-directory
# locks can prevent `rm -rf "${OUTDIR}"` from succeeding.
if [ -d "${OUTDIR}" ]; then
    find "${OUTDIR}" -mindepth 1 -delete
fi
rm -f "build/PuckUtilityApp-lin-${VERSION}.zip"

if [ -n "$PY" ]; then
    "$PY" -m PyInstaller --clean puckutilityapp.py --name PuckUtilityApp --onefile --distpath "${OUTDIR}" --add-data=lib/python3*/site-packages/canopen/:canopen/ --hiddenimport canopen --hiddenimport canopen.network --hiddenimport can --hiddenimport can.interfaces.socketcan
else
    pyinstaller --clean puckutilityapp.py --name PuckUtilityApp --onefile --distpath "${OUTDIR}" --add-data=lib/python3*/site-packages/canopen/:canopen/ --hiddenimport canopen --hiddenimport canopen.network --hiddenimport can --hiddenimport can.interfaces.socketcan
fi
cp -r images/ "${OUTDIR}"/
cp -r config/ "${OUTDIR}"/
cp puck4.eds "${OUTDIR}"/
cp system-config.ini "${OUTDIR}"/
cp flashloader.eds "${OUTDIR}"/
cp scripts/reset_can.sh "${OUTDIR}"/
cp scripts/60-can.rules "${OUTDIR}"/
cp canopen_runner.py "${OUTDIR}"/
cp flashp4.py "${OUTDIR}"/
cp cli_ops.py "${OUTDIR}"/
cp scripts/setup-socketcan.sh "${OUTDIR}"/
cp scripts/install-ubuntu.sh "${OUTDIR}"/
cp PuckUtilityApp.desktop "${OUTDIR}"/
cp PuckUtilityAppGuide.pdf "${OUTDIR}"/
cp -r firmware/ "${OUTDIR}"/
cd build/lin
zip -r "../PuckUtilityApp-lin-${VERSION}.zip" "PuckUtilityApp-lin-${VERSION}"
