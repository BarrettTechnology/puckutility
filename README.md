## PuckUtility
This is the repository for Barrett's PuckUtility App. 

## Description
This Python3 (wxpython) application is compatible with Barrett's P4 series of motor controllers. It can be used to:
* Find motor controllers on the CAN bus
* Change CAN IDs
* Update firmware
* Flash CAN-adapter (CandleLight / CANable) firmware
* Configure the CANopen Object Dictionary
* Calibrate the motor controller
* Test Profile Torque / Velocity / Position control

The app launches a GUI when run with no arguments, or runs headlessly when CLI
flags are provided (see [Command-line / headless mode](#command-line--headless-mode)).

## Installation
Set up the Python virtual environment and install dependencies.

### Linux
```
./scripts/setup-venv.sh
source scripts/activate
scripts/setup-pip.sh
```

### Windows
```
python -m venv .
Scripts\activate
pip install -r requirements.txt
```
To flash CAN-adapter firmware (`--flash-canable`) you also need `dfu-util` on
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
> DFU device is required for `--flash-canable` to work. Documentation for this
> step is coming soon.

## CAN adapter setup

### Linux (SocketCAN)
```
scripts/setup-socketcan.sh
```
Then plug in your Peak USB-CAN or CandleLight / CANable adapter. The adapter is
brought up as a SocketCAN interface (e.g. `can0`).

### Windows (PCAN)
On Windows, CAN access goes through PCAN. Pass the PCAN channel index to
`--can` (e.g. `--can 0` selects `PCAN_USBBUS1`).

### Flashing a CANable adapter
Flash the bundled CandleLight Multiboard firmware to an STM32G431 CANable over
USB DFU:
```
python3 puckutilityapp.py --flash-canable
```
To flash a specific firmware file instead:
```
python3 puckutilityapp.py --flash-canable path/to/custom.bin
```

## Usage (launch the GUI)
```
./puckutilityapp.py
```

## Command-line / headless mode
Running with CLI flags performs the requested operation without launching the
GUI. Must be run from the puckutility directory so that `puck4.eds` is
accessible.

| Flag | Description |
| --- | --- |
| `--can DEVICE` | CAN device (e.g. `can0` on Linux, `0` for `PCAN_USBBUS1` on Windows) |
| `--id ID [ID ...]` | One or more target node IDs |
| `--all` | Scan the bus and apply the operation to all discovered pucks |
| `--touchscreen` | GUI mode only: launch fullscreen (e.g. for the 7" Raspberry Pi touchscreen) |
| `--scan` | Scan the CAN bus and print all discovered node IDs |
| `--flash FIRMWARE` | Flash a firmware file (`.bin` or `.ebin`) to the target node(s) |
| `--config CSV` | Upload a motor configuration CSV file |
| `--calibrate` | Run full calibration (test_encoder, ibias, igainfactor, enczero) |
| `--system-config INI` | Apply a system configuration INI (handles firmware check, config upload, optional calibration) |
| `--flash-canable [FIRMWARE]` | Flash CandleLight Multiboard firmware via USB DFU (uses bundled firmware when no path is given) |
| `--verbose` | Show detailed output during `--flash-canable` |

Examples:
```
# Flash firmware to node 1
python3 puckutilityapp.py --can can0 --id 1 --flash firmware/P4-v1.1.5.bin

# Upload config CSV to nodes 1 and 2
python3 puckutilityapp.py --can can0 --id 1 2 --config config/motor.csv

# Calibrate all discovered pucks
python3 puckutilityapp.py --can can0 --all --calibrate

# Apply system config INI
python3 puckutilityapp.py --can can0 --system-config system.ini

# Flash bundled CandleLight Multiboard firmware to an STM32G431 canable via USB DFU
python3 puckutilityapp.py --flash-canable
```

## Puck Firmware
Download the latest Puck Firmware at [barrett.com/puck-firmware](https://barrett.com/puck-firmware)  
Place the .ebin files in puckutility/firmware 

## Support
For technical support, email support@barrett.com.

## Roadmap
* Tune current / velocity / position control gains
* Command square-wave / sinusoidal trajectories, or use a USB knob/slider
* Graph step-reponse and following error in realtime

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
