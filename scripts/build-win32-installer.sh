#!/usr/bin/env bash
cd ..
VERSION=$(grep -m1 'SetTitle' puckutilityapp.py | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')
OUTDIR="build/win/PuckUtilityApp-${VERSION}"

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
python -m PyInstaller "${PI_COMMON[@]}" --name PuckUtilityApp --noconsole

# Console build (stdout/stderr/input attach to the launching terminal).
python -m PyInstaller "${PI_COMMON[@]}" --name PuckUtilityAppCLI --console

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

# PCAN driver installer (downloaded fresh each build)
curl https://web.barrett.com/support/Puck_ControlLibrary/PeakOemDrv.exe -o PeakOemDrv.exe
mv PeakOemDrv.exe "${OUTDIR}"/

# Windows 10+ ships BSD tar; -a infers compression from the .zip suffix.
cd build/win
tar -a -cf "PuckUtilityApp-${VERSION}-win.zip" "PuckUtilityApp-${VERSION}/"
