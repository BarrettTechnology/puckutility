# PyInstaller spec for the Windows build (scripts/build-win32-installer.sh).
#
# One Analysis, two executables, one folder:
#   PuckUtilityApp.exe     windowed (Start menu / desktop shortcut, no console)
#   PuckUtilityAppCLI.exe  console (`--scan / --flash / --config / ...`)
# Both share the _internal/ runtime, so the installer carries Python, wx,
# numpy and matplotlib once. One-dir rather than one-file: no unpack to %TEMP%
# on every launch (seconds faster, and far fewer antivirus false positives).
#
# Run from the repo root:  python -m PyInstaller scripts/windows/PuckUtilityApp.spec
import os
import canopen
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, '..', '..'))

# libusb-package carries libusb-1.0.dll, which the CANable path (pyusb ->
# WinUSB) loads at run time; PyInstaller cannot see that dependency itself.
usb_datas, usb_binaries, usb_hidden = collect_all('libusb_package')

a = Analysis(
    [os.path.join(ROOT, 'puckutilityapp.py')],
    pathex=[ROOT],
    binaries=usb_binaries,
    datas=[(os.path.dirname(canopen.__file__), 'canopen')] + usb_datas,
    hiddenimports=[
        'canopen', 'canopen.network',
        'can', 'can.interfaces.pcan',          # Peak: PCAN-Basic (PCANBasic.dll)
        'p4core.can_backend', 'p4core.candlelight_bus',   # CANable: WinUSB
        'usb', 'usb.core', 'usb.backend.libusb1',
        'puckutility.gui.app',
        'puckutility.controllers.canable',
    ] + usb_hidden,
    excludes=['tkinter'],
)
pyz = PYZ(a.pure)

icon = os.path.join(ROOT, 'puckutility', 'data', 'images', 'BarrettIcon.ico')


def exe(name, console):
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name,
               console=console, icon=icon, upx=False)


gui = exe('PuckUtilityApp', console=False)
cli = exe('PuckUtilityAppCLI', console=True)

coll = COLLECT(gui, cli, a.binaries, a.datas, name='PuckUtilityApp', upx=False)
