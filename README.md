## PuckUtility
This is the repository for Barrett's PuckUtility App. 

## Description
This Python 3 application is compatible with Barrett's P4 series of motor
controllers. It runs as a wxPython GUI, or as a command line that needs no
display (and no wxPython). It can be used to:
* Find motor controllers on the CAN bus, and read each one's firmware,
  flashloader, model and live readings
* Change CAN IDs (the PDO COB-IDs follow)
* Update firmware (`.bin` / `.ebin`), one puck or every puck on the bus
* Flash CAN-adapter (CandleLight / CANable) firmware
* Load, export and check configuration CSVs (the CANopen Object Dictionary)
* Bring a whole system to a set firmware and configuration from a
  system-config INI
* Calibrate each actuator (the per-unit steps a batch configuration CSV
  cannot carry)
* Test Profile Torque / Velocity / Position control, and homing

Gain tuning and motor identification are pucktuner's (and p4gui's), not
puckutility's.

## Installation
The code shared with p4gui and pucktuner is the `p4core` submodule; fetch it
first:
```
git submodule update --init p4core
```

### Linux
```
./scripts/setup-venv.sh
source scripts/activate
./scripts/setup-pip.sh
```

### Windows
```
python -m venv .
Scripts\activate
pip install -r requirements.txt
pip install -e ./p4core -e .
```
To flash CAN-adapter firmware (`flash-canable`) you also need `dfu-util` on
your PATH:
```
choco install dfu-util
```
or
```
scoop install dfu-util
```
(or download it from https://dfu-util.sourceforge.net/ and add it to PATH).
The `libusb-package` dependency bundles the libusb DLL, so there is no need to
install libusb system-wide.

> ⚠️ **USB driver setup (Windows):** binding the WinUSB driver to the CANable /
> DFU device is required for `flash-canable` to work. Documentation for this
> step is coming soon.

### Command line only (no wxPython)
```
git submodule update --init p4core
pip install ./p4core .
```
`puckutility --help` lists the commands. Only the GUI needs wxPython and
matplotlib (`pip install ".[gui]"`); without them, starting the GUI prints
that install line instead.

## CAN adapter setup

### Linux (SocketCAN)
```
scripts/setup-socketcan.sh
```
Then plug in your Peak USB-CAN or CandleLight / CANable adapter. The adapter is
brought up as a SocketCAN interface (e.g. `can0`). The Linux installer (see
[Building a release](#building-a-release)) sets this up for you.

### Windows (PCAN)
On Windows, CAN access goes through PCAN. `--can` takes the channel index
(`--can 0` selects `PCAN_USBBUS1`), a `canN` name (`can0` is also
`PCAN_USBBUS1`) or a full `PCAN_USBBUSn` name.

### Flashing a CANable adapter
Flash the bundled CandleLight Multiboard firmware to an STM32G431 CANable over
USB DFU:
```
puckutility flash-canable
```
To flash a specific firmware file instead:
```
puckutility flash-canable path/to/custom.bin
```

## Usage (launch the GUI)
```
./puckutilityapp.py              # from the checkout
puckutility                      # once installed (setup-pip.sh installs it)
puckutility-gui                  # the GUI only (no console window on Windows)
puckutility --touchscreen        # fullscreen (e.g. a 7" Raspberry Pi touchscreen)
puckutility --can can1           # preselect a CAN port
```

The main window:
* **CAN Port / Scan Pucks / Select ID** — find the pucks and pick one
* **New ID / Set** — change the selected puck's node ID
* **Version / Update...** — its firmware version, and flash new firmware
* **Configure / Browse...** — load a configuration CSV
* **Test** — Idle, Torque (mNm), Velocity (RPM), Position (deg) or Homing,
  with a value and Go
* Live bus current, bus voltage, puck and motor temperature, and RPM

Drop a `.bin` / `.ebin` (firmware), `.csv` (configuration) or `.ini`
(system config) file onto the window to use it.

The **Menu** menu has Scan Pucks, Update All Pucks (Firmware), Upload System
Config, the ADC monitor, Actuator Calibration (Active Puck / All Pucks) and
each Actuator Calibration Step on its own, Encoder Error Compensation, Set
User Direction and Help. Only the calibration steps the selected puck's
firmware can run are listed. **Log > Open Log...** shows the session log.

### Working folders
`firmware/`, `config/`, `logs/` and `system-config.ini` are looked up in the
repository root when run from a checkout, beside the executable in a release
build, and in `~/.local/share/puckutility` after a `pip install`. A
system-config INI names its firmware and CSV files by filename, from
`firmware/` and `config/`.

## Command line
Every operation is also a command, and needs no display:

```
puckutility --can can0 scan
puckutility --can can0 --id 1 info            # --all for every puck; --no-flashloader
puckutility --can can0 --id 1 flash firmware/P4-4.4.0.ebin   # --all for every puck
puckutility --can can0 --id 1 config load config/motor.csv   # save and reboot
puckutility --can can0 --id 1 config export motor.csv        # the puck's values
puckutility config check config/motor.csv     # validate, no puck needed
puckutility config templates                  # the shipped templates
puckutility --can can0 --all calibrate        # actuator calibration; --save
puckutility --can can0 --id 1 calibrate slope fold
puckutility --can can0 system-config system-config.ini
puckutility --can can0 --id 5 set-id 7
puckutility --can can0 --id 5 reset           # NMT reset, wait for it
puckutility flash-canable [FIRMWARE]
puckutility --help                            # and: puckutility COMMAND --help
```

`--can` defaults to `$P4_CAN`, else `can0`. `--id` may be left out when there
is one puck on the bus. `--fd` requires CAN FD, `--classic` uses classic CAN
only. `-y` answers yes to every question (calibration asks before the motor
turns), `-q` prints only results, warnings and errors, `-v` more, and
`--json` prints results as JSON. Exit status: 0 done, 1 failed, 2 usage,
3 no adapter/puck, 130 stopped.

`calibrate` runs the actuator calibration the GUI's Actuator Calibration
menu runs (p4core.actuator's steps): encoder, bias, gain, slope, fold,
winding, runout, cogging (`puckutility calibrate --help`); without STEP
arguments, the ones the puck's firmware can run.  Load the batch CSV first.
The motor turns, continuously for cogging.  Results stay in RAM unless
`--save` (Save.All when every step passed).

### The old flags
The flags of earlier versions still work and are translated to the commands
above; `--id 1 2 3` runs the command once per puck, one after the other:

| Flag | Command |
| --- | --- |
| `--scan` | `scan` |
| `--info` | `info` (every puck unless `--id`) |
| `--flash FIRMWARE` | `flash FIRMWARE` |
| `--config CSV` | `config load CSV` |
| `--calibrate` (`--quick` is ignored) | `calibrate` |
| `--calibrate-slope` | `calibrate slope` |
| `--system-config INI` | `system-config INI` |
| `--set-id NEW_ID` (with `--id CURRENT`) | `set-id NEW_ID` |
| `--flash-canable [FIRMWARE]` | `flash-canable [FIRMWARE]` |

```
python3 puckutilityapp.py --can can0 --id 1 2 --config config/motor.csv
```

### Layout
`puckutility/` is the package: `model.py` (the CAN session, from p4core),
`controllers/` (the operations, UI-free), `cli.py` (the commands), `gui/`
(the wx GUI). Code shared with p4gui and pucktuner (flashing, config CSVs,
the CAN backends, the object dictionary, the actuator calibration) is in the
`p4core` submodule.

## Building a release

Every build ships the puck firmware that `system-config.ini` names, from
`firmware/` (or `FIRMWARE_DIR`); the build stops early if it is missing.

### Linux

Requires Docker. Your user must be in the `docker` group:
```
sudo usermod -aG docker $USER
```
then log out and back in (or prefix the build commands below with
`sg docker -c '…'` to apply the group without re-logging in).

The build runs inside a pinned Ubuntu 20.04 container so the result runs on
Ubuntu 20.04 → 26.04+ regardless of your host OS. The first build also builds
the `puckutility-builder` image — this downloads the prebuilt wxPython wheel
(no compilation) and takes a few minutes; add `--rebuild-docker` to the first
build, and again whenever `requirements.txt` changes. Later builds use
`--docker` and are fast. Add `--clean-pyinstaller` if the binary seems stale
after adding new imports.

**Installer (recommended):**
```
./scripts/build-linux-installer.sh --docker --installer
```
Output: `build/PuckUtilityApp-lin-vX.Y.Z-installer.run`, a
self-extracting installer. On the target machine:
```
sh ./PuckUtilityApp-lin-vX.Y.Z-installer.run
```
It asks which CAN adapters to support (Peak PCAN-USB, CANable; desktop or
text questions), asks for sudo / pkexec, and installs the app plus the
`puck-can-common` / `puck-can-peak` / `puck-can-canable` packages (udev
rules, automatic `can0`, `can1`, … bring-up, `dfu-util` for CANable). It
installs `linux-modules-extra` when the kernel lacks the adapter driver.
Run it again to change the adapter selection. Options: `--peak` /
`--no-peak`, `--canable` / `--no-canable`, `-y`, `--text`, `--dry-run`,
`--uninstall` (`--help` for all).

**.deb packages only:**
```
./scripts/build-linux-installer.sh --docker --deb
```
Output: `build/deb/puckutilityapp_X.Y.Z_amd64.deb` and the CAN support debs
(`scripts/build-can-debs.sh`). Install with
`sudo apt install ./puckutilityapp_X.Y.Z_amd64.deb` (plus the CAN debs you
need).

**PyInstaller zip (legacy):**
```
./scripts/build-linux-installer.sh --pyinstaller
```
Output: `build/PuckUtilityApp-lin-vX.Y.Z.zip`

### Windows (10 / 11, x64)

The build is a Bash script (PyInstaller, the data files, the PEAK driver,
the zip and the installer). PowerShell cannot execute a `.sh` file directly,
and a bare `bash` resolves to WSL — which is usually not installed. Run it
through **Git Bash** instead (bundled with
[Git for Windows](https://gitforwindows.org/)).

From a PowerShell prompt:
```
& "C:\Program Files\Git\bin\bash.exe" ./scripts/build-win32-installer.sh
```

Or open a **Git Bash** terminal and run it directly:
```
./scripts/build-win32-installer.sh
```

Outputs (`build/win/`):
* `PuckUtilityApp-win32-vX.Y.Z/` — the app folder: `PuckUtilityApp.exe`
  (windowed GUI) and `PuckUtilityAppCLI.exe` (console)
* `PuckUtilityApp-win32-vX.Y.Z.zip` — a portable copy
* `PuckUtilityApp-win-vX.Y.Z-setup.exe` — the installer, built when
  [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed
  (`scripts/windows/puckutility.iss`)

The installer has app, PEAK PCAN driver and CANable components. The PEAK
driver setup is bundled (or downloaded at install time with
`--no-bundle-peak`) and skipped when PCAN-Basic is already installed. CANable
needs no driver (WinUSB); the installer links STM32CubeProgrammer, which
cannot be redistributed. Users can write to `firmware/`, `config/`, `logs/`
and `system-config.ini` beside the exe.

Notes:
- You do **not** need to activate the venv first — the script locates
  `Scripts/python.exe` itself (or set `PYTHON`).
- Options: `--no-bundle-peak`, `--refresh-drivers` (re-download the PEAK
  driver), `--no-setup` (stop after the zip). Environment: `ISCC` (the Inno
  Setup compiler), `SIGNTOOL` (sign the setup), `FIRMWARE_DIR`.
- An internet connection is needed the first time (the PEAK driver is cached
  afterwards).

## Testing across Ubuntu versions

`scripts/test-cross-ubuntu.sh` validates both the dev environment and the
shipped installer on every supported Ubuntu release. For each version it spins
up a clean container, runs this repo's own `scripts/setup-venv.sh`, then
verifies the GUI stack actually initialises (`import wx` + `wx.App()` under a
headless X server) and that every declared dependency imports. It also builds
the `.run` installer once, runs it unattended in each container, checks the
CAN adapter options (select, deselect, uninstall) and launches the installed
app. It exercises the *real* scripts, so run it whenever you touch
`setup-venv.sh` / `requirements.txt` / the installer, or when a new Ubuntu
ships.

Requires Docker, with your user in the `docker` group:
```
sudo usermod -aG docker $USER
```
then log out and back in (or prefix the commands below with `sg docker -c '…'`
to apply the group without re-logging in).

```
scripts/test-cross-ubuntu.sh                 # 20.04 22.04 24.04 26.04
scripts/test-cross-ubuntu.sh 24.04 26.04     # a subset / a new release
```

Nothing is installed on your host and nothing pops up on screen — it all runs
headless inside the containers, independent of your local `.venv`. The first run
pulls the base images and downloads the standalone Python 3.13 + wxPython /
scientific wheels (cached under `/tmp` afterward, shared between puckutility and
pucktuner), so it takes a few minutes; later runs are fast. It prints a
per-release `PASS`/`FAIL` summary and exits non-zero if any release fails — on a
failure, scroll up to that release's banner for the Python traceback.

## Puck Firmware
Download the latest Puck Firmware at [barrett.com/puck-firmware](https://barrett.com/puck-firmware)  
Place the .ebin files in `firmware/` (see [Working folders](#working-folders)).

## Support
For technical support, email support@barrett.com.

## Roadmap
* Tune Gains and Save to CSV (in the menu, greyed out until implemented)
* The Factory menu (J-Link flashing and board tests), hidden for now

## Contributing
Pull requests are welcome!

## Authors and acknowledgment
Special thanks to Bailey Noack and Brian Zenowich for their contributions to this code!

## License
Copyright 2026, Barrett Technology

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS “AS IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## Project status
The project is active.
