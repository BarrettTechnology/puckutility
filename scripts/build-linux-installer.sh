#!/usr/bin/env bash
cd ..
pyinstaller --clean puckutilityapp.py --name PuckUtilityApp --onefile --distpath build/lin/PuckUtilityApp --add-data=lib/python3*/site-packages/canopen/:canopen/ --hiddenimport canopen --hiddenimport canopen.network --hiddenimport can --hiddenimport can.interfaces.socketcan
cp -r images/ build/lin/PuckUtilityApp/
cp -r config/ build/lin/PuckUtilityApp/
cp puck4.eds build/lin/PuckUtilityApp/
cp setup-config.ini build/lin/PuckUtilityApp/
cp flashloader.eds build/lin/PuckUtilityApp/
cp scripts/reset_can.sh build/lin/PuckUtilityApp/
cp scripts/60-can.rules build/lin/PuckUtilityApp/
cp canopen_runner.py build/lin/PuckUtilityApp/
cp flashp4.py build/lin/PuckUtilityApp/
cp scripts/setup-socketcan.sh build/lin/PuckUtilityApp/
cp scripts/install-ubuntu.sh build/lin/PuckUtilityApp/
cp PuckUtilityApp.desktop build/lin/PuckUtilityApp/
cp PuckUtilityAppGuide.pdf build/lin/PuckUtilityApp/
cp -r firmware/ build/lin/PuckUtilityApp/ 
cd build/lin
zip -r ../PuckUtilityApp-lin.zip PuckUtilityApp 
