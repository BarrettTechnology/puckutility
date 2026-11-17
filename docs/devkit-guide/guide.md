---
title: Puck® P4™ Quick Start Manual
intro: Follow this manual to get the P4 Dev Kit up and running with ease.
document: B7296
revision: AE
app: Puck Utility
app_version: from ../../puckutility/version.py
logo: logo-drive-systems.png
output: P4-Dev-Kit-Quick-Start-Manual.pdf
phone: (617) 252-9000
web: barrett.com
email: support@barrett.com
# screenshots (scripts/build-guide.sh): the real app against the puck simulator on vcan0
app_main: puckutility.gui.app:main
app_args: --can vcan0
app_root: ../..
ui_sources: ../../puckutility/gui
startup_s: 3
---

```shot main
f.can_port(None, silent=True)
f.scan_pucks(None)
wait 3
f.text_version.SetValue('5.0.5')
f.choice_test.SetStringSelection('Vel (RPM)')
f.text_testvalue.SetValue('30')
f.frame_statusbar.SetStatusText('Ready', 1)
wait 1
```

<!-- row -->

![Dev Kit and accessories](img:devkit.png | power-adapter.png serial-cable.png peak-can-adapter.png width=4.3in)

# ASSEMBLY
## Hardware

1. **Thumb screw** - remove it to let the motor spin freely (optional). [@pt:88.6,74:below]

## Electrical

2. **Power** - plug the 24 V adapter's barrel jack into J16 on the Dev Board. [@pt:22.3,68.5:left]
3. **CAN cable** - attach the male end of the serial cable to J1 on the Dev Board. [@pt:37.4,79.5:below]
4. **CAN adapter** - connect the cable's female end to the PEAK CAN adapter and hand-tighten. [@img:serial-cable.png:above]
5. **Mains** - plug the 24 V adapter into an outlet (110-220 V AC) to power the board. [@img:power-adapter.png:above]
6. **USB** - connect the CAN adapter to a USB port on the computer. [@img:peak-can-adapter.png:above]
   The Dev Board makes its 12 V logic supply from the 24 V input.

<!-- /row -->

-> Software installation

<!-- page -->

# SOFTWARE INSTALLATION
## Puck Utility, from barrett.com/puck-software

<!-- row -->

# WINDOWS
## Windows 10 / 11

1. **Download** - the Puck Utility installer, PuckUtilityApp-win-...-setup.exe.
2. **Install** - run it and keep Puck Utility and the Peak PCAN-USB driver selected.
   The Peak driver is skipped when it is already installed.
3. **Connect** - plug the CAN adapter in once the install is done.
4. **Launch** - Puck Utility from the Start menu (or the desktop icon, if chosen).
   The Start menu also has the Puck Utility User Guide.

# LINUX
## Ubuntu 20.04 to 26.04

1. **Download** - the Puck Utility installer, PuckUtilityApp-lin-...-installer.run.
2. **Install** - double-click it, or run `sh ./PuckUtilityApp-lin-...-installer.run` in a terminal.
3. **CAN adapter** - choose Peak PCAN-USB when asked, then enter your password.
   The installer sets up the driver and brings can0 up when the adapter is plugged in.
4. **Launch** - Puck Utility from the applications menu or the desktop icon.

<!-- /row -->

-> Puck Utility App

<!-- page -->

![Puck Utility main window](shot:main)

# PUCK UTILITY APP
## Main Features

1. **Menu** - scanning, firmware updates, calibration and settings. [@menu:Menu]
   The Dev Kit comes configured and calibrated: calibrate only after changing the motor.
2. **CAN Port** - selects the CAN adapter (can0 to can3). [@choice_port:row]
   Several CAN buses can run on one computer: pick the one the Puck is on.
3. **Scan Pucks** - finds every Puck on the bus and selects the lowest ID. [@button_1:above]
   Scan runs at startup. Scan again if power or CAN was connected later.
4. **Select ID** - chooses the active Puck from the Pucks found. [@choice_id:row]
   Every control in this window acts on the active Puck.
5. **New ID** - enter a new ID (1-127) and press Set. [@text_id:row]
   CANopen node IDs are 7-bit: 1 to 127, one per Puck on the bus.
6. **Version** - the active Puck's firmware. Update... loads new firmware. [@text_version:row]
   Firmware files are .bin or .ebin. Keep power and CAN connected while it loads.
7. **Configure** - Browse... uploads a motor configuration (.csv) to the Puck. [@button_10:row]
   You can also drop the .csv on the window. Calibrate after a new configuration.
8. **Mode** - Idle, Torque (mNm), Velocity (RPM), Position (deg) or Homing. [@choice_test:below]
   Input limits come from the Puck's configuration.
9. **Go** - sends the value to the Puck; each press sends it again. [@button_6:below]
   In Position mode, Go adds the value to the current target.
10. **Position Dial** - motor shaft position while the ADC Monitor is on. [@Dial:below]
    The dial shows the motor shaft, not the gearhead output.
11. **ON/OFF** - turns the ADC Monitor on and off. [@onoff1:right]
    The monitor polls the Puck; turn it off on slow computers.
12. **ADC Monitor** - current, Puck temperature, motor temperature and RPM. [@VBus:right]
    Readings update while the monitor is on.

-> Troubleshooting

<!-- page -->

### No CAN bus found!
! The CAN network could not start: a missing driver or a problem with the CAN adapter.
+ Check that the PEAK adapter is plugged in and its driver is installed (run the installer again if not), then choose the right CAN Port.

### No Pucks found!
! The CAN network started, but no Puck answered.
+ Check the serial cable at J1 and the 24 V supply at J16 (5 V and 3.3 V LEDs on). Unplug the CAN adapter, plug it back in, and scan again.

### Motor not spinning
! A command is sent but the motor does not turn, or it cogs badly.
+ Check the bus voltage on the ADC Monitor, then run Menu > Actuator Calibration > Active Puck.

### Calibration reminder
! "Actuator calibration is required" after a calibration: a step did not pass.
+ The reminder names the step. Fix the cause it gives and run that step again from Actuator Calibration Steps.

### Other messages
**ID already in use!** - every Puck on the bus needs its own ID.
**No active node!** - select a Puck (Select ID) before choosing a mode.
**No input value!** - enter a value before pressing Go.

### Still stuck?
If a new issue appears, contact Barrett support with the Puck ID, the firmware version and what the app log shows.
