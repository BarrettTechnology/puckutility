## PuckUtility
This is the repository for Barrett's PuckUtility App. 

## Description
This Python3 (wxpython) application is compatible with Barrett's P4 series of motor controllers. It can be used to:
* Find motor controllers on the CAN bus
* Change CAN IDs
* Update firmware
* Configure the CANopen Object Dictionary
* Calibrate the motor controller
* Test Profile Torque / Velocity / Position control

## Installation (set up the Python virtual environment and install dependencies)
```
scripts/setup-venv.sh
source bin/activate
scripts/setup-pip.sh
```

## Install the CAN Driver (Linux)
```
scripts/setup-socketcan.sh
```
Then, plug in your Peak USB-CAN hardware.

## Usage (launch the app)
```
./puckutilityapp.py
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
Copyright 2025, Barrett Technology

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS “AS IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## Project status
The project is active.
