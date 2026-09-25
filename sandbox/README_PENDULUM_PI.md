# Furuta pendulum — Raspberry Pi demo

Controller: Furuta CST GUI v4.1 (from `feature/cm-control-updates`, 4f69fcd), energy
swing-up + PID balance in Cyclic-Sync Torque mode. Puck 1 = arm motor, Puck 2 = pendulum
encoder, via a CANable (candleLight) at 1 Mbit on `can0`.

| File | What it is |
|---|---|
| `furuta_pendulum.py` | v4.1 controller. No flag = engineering GUI (gains, scan, zero, bias). `--touchscreen` = customer kiosk. |
| `furuta_kiosk.py` | The kiosk screen. Reuses the v4.1 control loop and CAN/drive code unchanged. |
| `boot_prompt.py` | Full-screen Barrett logo + "Start the pendulum demo?" YES / NO. |
| `kiosk_widgets.py` | Logo panel with long-press, big touch buttons, and the display environment settings. |
| `pendulum-launch.sh` | `prompt` \| `kiosk` \| `gui`. Logs to `~/.cache/barrett-pendulum/pendulum.log`. |
| `setup-pi.sh` | One-time Pi setup, plus `--check` diagnostics. |

## Setting up the Pi

```sh
git clone git@git.barrett.com:software/puckutility.git   # or git pull + checkout
cd puckutility && git checkout feature/pendulum-pi
./sandbox/setup-pi.sh          # as the desktop user, not sudo
sudo reboot
```

`setup-pi.sh` works on Ubuntu (GNOME) and Raspberry Pi OS, and is safe to re-run. It:
installs wxPython from apt and puts canopen in `sandbox/.venv`; sets up CAN (same udev and
`can-up@.service` files as puckutility, and loads `gs_usb` at boot); adds the login autostart
for the boot prompt; turns off screen blanking, locking, sleep and notification banners;
sets the Barrett wallpaper; turns on desktop auto-login; and adds app-menu entries for the
kiosk and the engineering GUI.

If something isn't working: `./sandbox/setup-pi.sh --check`. It shows the OS and desktop,
Python/wx/canopen, the CANable, `can0` bitrate/state/traffic, the kiosk settings, and the
last log lines.

## At the booth

- **Power on** → Barrett logo prompt → **YES** starts the kiosk; **NO** leaves the desktop.
  (`pendulum-launch.sh prompt --auto-yes 20` auto-picks YES after 20 s, if wanted.)
- The kiosk connects by itself and retries forever. If it can't connect after a few tries,
  a **CONNECT** button appears.
- It zeroes the pendulum once it hangs still, then **START** swings it up and balances it.
  **STOP** = zero torque (arm goes limp). Each run also **stops itself after 60 s** and
  goes back to START. Change it with `--auto-stop SECONDS` (`0` = never), e.g. in the
  autostart entry `~/.config/autostart/barrett-pendulum.desktop`:
  `Exec=…/pendulum-launch.sh prompt --auto-stop 120`.
- If something goes wrong (drive fault, CAN send failure, lost CAN), the pendulum stops
  and the screen says "Stopped — tap START to try again". START clears the drive fault. If
  the motor gets too hot (≥85 °C), the kiosk cools down and comes back below 70 °C.
- **Staff menu:** hold the Barrett logo (top left) for 5 s → Exit to desktop / Reboot /
  Shut down. It closes itself after 30 s. After "Exit to desktop", *Barrett Pendulum* in
  the app menu starts the kiosk again.

Gains are fixed at the v4.1 defaults in `furuta_pendulum.py` (`KP_DEFAULT` …). To re-tune,
use the engineering GUI and copy the values back into the defaults.
