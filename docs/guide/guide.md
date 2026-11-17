---
title: Puck Utility App Guide
intro: Follow this guide to navigate the Puck Utility App with ease. Page 2 covers the menu, page 3 troubleshooting.
document: B7372
revision: AB
app: Puck Utility
app_version: from ../../puckutility/version.py
logo: logo-drive-systems.png
output: ../../PuckUtilityAppGuide.pdf
phone: (617) 252-9000
web: barrett.com
email: support@barrett.com
# screenshots (python -m p4guide shots): the real app against the puck simulator on vcan0
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

![Puck Utility main window](shot:main)

# PUCK UTILITY APP
## Main Features

1. **Menu** - scanning, firmware updates, calibration and settings. [@menu:Menu]
   See page 2 for every menu item.
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

<!-- page -->

![Menu](menu:Menu@main)

# MENU
## Setup, firmware and calibration

1. **Scan Pucks** - the same as the Scan Pucks button (Ctrl+S). [@item:Scan Pucks]
2. **Update All Pucks (Firmware)** - loads one firmware file onto every Puck found. [@item:Update All Pucks (Firmware)]
   Keep power and CAN connected until every Puck reports done.
3. **Upload System Config** - applies a system configuration (.ini) to the Pucks. [@item:Upload System Config]
4. **ON/OFF ADC Monitor** - the same as the ON/OFF switch (Ctrl+P). [@item:ON/OFF ADC Monitor]
5. **Actuator Calibration** - calibrates the Active Puck (Ctrl+C) or All Pucks. [@item:Actuator Calibration]
   Runs every step below. The motor moves: keep the output free to turn.
6. **Actuator Calibration Steps** - runs one calibration step on its own. [@item:Actuator Calibration Steps]
   Use after changing one thing, e.g. the encoder or the friction.
7. **Encoder Error Compensation** - turns the encoder runout correction on or off. [@item:Encoder Error Compensation]
   Needs the encoder runout table (step 7) and firmware that supports it.
8. **Set User Direction...** - sets which way is positive at the output. [@item:Set User Direction...]
9. **Help** - opens the Barrett support page. [@item:Help]

<!-- page -->

### No CAN bus found!
! The CAN network could not start: a missing driver or a problem with the CAN adapter.
+ Check that the CAN adapter is plugged in and its driver is installed, then choose the right CAN Port.

### No Pucks found!
! The CAN network started, but no Puck answered.
+ Check the cables and Puck power (5 V and 3.3 V LEDs on). Unplug the CAN adapter, plug it back in, and scan again.

### Motor not spinning
! A command is sent but the motor does not turn, or it cogs badly.
+ Check the bus voltage on the ADC Monitor, then run Menu > Actuator Calibration > Active Puck. Make sure the right configuration is loaded.

### Calibration reminder
! "Actuator calibration is required" after a calibration: a step did not pass.
+ The reminder names the step. Fix the cause it gives and run that step again from Actuator Calibration Steps.

### Other messages
**ID already in use!** - every Puck on the bus needs its own ID.
**No active node!** - select a Puck (Select ID) before choosing a mode.
**No input value!** - enter a value before pressing Go.

### Still stuck?
If a new issue appears, contact Barrett support with the Puck ID, the firmware version and what the app log shows.
