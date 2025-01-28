#!/bin/sh
cd ..
python -m PyInstaller --clean puckutilityapp.py --name PuckUtilityApp --distpath build/win/PuckUtilityApp --onefile --hiddenimport can.interfaces.pcan --icon=images/BarrettIcon.ico 
cp -r images build/win/PuckUtilityApp/
cp -r config build/win/PuckUtilityApp/
cp puck4.eds build/win/PuckUtilityApp/
#cp flashp4.py build/win/PuckUtilityApp/
#cp canopen_runner.py PuckUtilityApp/
#cp flashloader.eds PuckUtilityApp/
#cp -r firmware PuckUtilityApp/
curl https://web.barrett.com/support/Puck_ControlLibrary/PeakOemDrv.exe -o PeakOemDrv.exe
mv PeakOemDrv.exe build/win/PuckUtilityApp/
cd build/win
Tar -a -cf PuckUtilityApp.zip PuckUtilityApp/
