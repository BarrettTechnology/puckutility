#!/usr/bin/env bash
cd ..
VERSION=$(grep -m1 'SetTitle' puckutilityapp.py | grep -oP 'v[0-9]+\.[0-9]+\.[0-9]+')
OUTDIR="build/lin/PuckUtilityApp-${VERSION}"
pyinstaller --clean puckutilityapp.py --name PuckUtilityApp --onefile --distpath "${OUTDIR}" --add-data=lib/python3*/site-packages/canopen/:canopen/ --hiddenimport canopen --hiddenimport canopen.network --hiddenimport can --hiddenimport can.interfaces.socketcan
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
zip -r "../PuckUtilityApp-${VERSION}-lin.zip" "PuckUtilityApp-${VERSION}"
