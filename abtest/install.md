# Installing the tools

The A/B test needs the **command line**: the `puckutility` and `p4core`
commands, plus a Python that can import `p4core`, `numpy` and `matplotlib`
for `ab_testing.py`. The GUI (Puck Utility) is optional. It is useful for
looking at a puck, but no step of the A/B needs it.

What the package carries:
```
installers/linux/     PuckUtilityApp-lin-v<ver>-installer.run   GUI + CAN support packages
installers/windows/   PuckUtilityApp-win-v<ver>-setup.exe       GUI + adapter drivers
wheels/               p4core-<ver>-py3-none-any.whl, puckutility-<ver>-py3-none-any.whl
ab_testing.py  testp4.py  csvdiff.py                          keep these three together
```

## 1. CAN adapter support

**Linux.** Run the installer, even if you do not want the GUI. It installs the
CAN support packages: automatic link bring-up, the adapter udev rules,
`set-can-iface.sh`, `reset_can.sh`, and `dfu-util` for the CANable.
```
$ bash PuckUtilityApp-lin-v<ver>-installer.run          # asks which adapters
$ bash PuckUtilityApp-lin-v<ver>-installer.run --yes    # both adapters, no questions
```
It asks for the sudo password itself. Options:
- `--peak` / `--no-peak`, `--canable` / `--no-canable`: choose the adapters.
- `--text`: questions in the terminal.
- `--dry-run`: show what would be done.
- `--uninstall`: remove everything.
- `--help`: list the options.

Ubuntu 20.04 to 26.04, x86_64. Re-running the installer changes the adapter
selection. Then plug the adapter in and check `ip -d link show can0`
(`can-adapters.md`).

**Windows.** Run `PuckUtilityApp-win-v<ver>-setup.exe`. Its components:
- **peak**: PEAK's driver and the PCAN-Basic API (`PCANBasic.dll`). Needed
  for a PCAN-USB.
- **canable**: nothing to install for the adapter itself, because Windows
  binds WinUSB on first plug-in. It offers STM32CubeProgrammer, which is
  only needed to flash the CANable's firmware.
- **addtopath** (unticked by default): puts `PuckUtilityAppCLI.exe` on the
  PATH.

Silent: `setup.exe /VERYSILENT /COMPONENTS="app,peak,canable"`.

## 2. The command line (Python)

Python 3.9 or newer, in a virtual environment. The first install downloads
`canopen`, `python-can`, `semver`, `numpy` and `matplotlib` from PyPI.

**Linux.** Ubuntu 22.04 and later ship Python 3.10+. On Ubuntu 20.04 the
system Python is 3.8, so use the `python3.9` package:
```
$ sudo apt install python3.9 python3.9-venv        # 20.04 only
$ python3.9 -m venv ~/p4ab                          # python3 -m venv on 22.04+
$ . ~/p4ab/bin/activate
(p4ab) $ pip install --upgrade pip
(p4ab) $ pip install wheels/p4core-*.whl wheels/puckutility-*.whl matplotlib
```

**Windows** (Python from python.org; tick "Add python.exe to PATH"):
```
> py -3 -m venv %USERPROFILE%\p4ab
> %USERPROFILE%\p4ab\Scripts\activate
(p4ab) > python -m pip install --upgrade pip
(p4ab) > for %w in (wheels\*.whl) do pip install %w
(p4ab) > pip install matplotlib pyusb libusb-package
```
`pyusb` and `libusb-package` are what a CANable needs on Windows. A PCAN
needs the PEAK driver from step 1 instead.

Every later session starts with the `activate` line.

## 3. Check

```
(p4ab) $ puckutility --version
(p4ab) $ python -c "import p4core, numpy, matplotlib; print('ok')"
(p4ab) $ puckutility --can can0 scan        # Windows: --can 0
(p4ab) $ python ab_testing.py --help
```
`scan` lists the node IDs on the bus. `puckutility --can can0 --id N version`
gives one puck's firmware. An empty list with the link up means no puck
answered: check power, termination and bitrate (`can-adapters.md`). An error
opening the port means the adapter or its driver is missing.

## Running from a source checkout instead

For development only. The package does not need it.
```
$ git clone <puckutility> && cd puckutility && git submodule update --init
$ python3 -m venv .venv && . .venv/bin/activate
$ sh setup-pip.sh        # requirements.txt, then p4core and puckutility editable
```
