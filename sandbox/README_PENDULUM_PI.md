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
| `disable-onscreen-keyboard.sh` | Turns off every on-screen keyboard (xvkbd, onboard, squeekboard, GNOME's). `--check` = report only. |
| `boot-speed.sh` | Why the Pi boots slowly (`systemd-analyze` report, saved for pasting). `--fix` disables the usual culprits (network-wait-online, cloud-init, ModemManager). |

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
  **STOP** = zero torque (arm goes limp). Auto-stop is **off by default**; with `--auto-stop SECONDS` each run stops itself and
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

## Future work

**Gentler automatic swing-up (4–6 swings instead of one hard one).** From rest the v4.1
energy controller pumps full ±Ks bang-bang every half-swing, so the first swing-up is violent
(the arm can whip round, the pendulum can fly over the top). Tried on 2026-09-25 and backed
out: a torque soft-start (too weak to start from rest) and an arm speed limit (didn't stop it).
Plan, to tune at the rig with SSH access (`sandbox/pi-connect.sh`):
1. **Ramp the energy target** (small swing → upright over ~4–6 s) instead of always aiming at
   upright energy, plus a small start kick from dead rest.
2. **Arm-centring spring** during swing-up so the arm oscillates about home instead of running away.
3. **Smooth energy law** (torque ∝ energy error × ω·cos θ, capped) instead of bang-bang, and the
   faster velocity estimate for push timing.
4. **Safety monitor** that parks automatically if the arm passes ~1 rev from home, spins too fast,
   or the pendulum goes over the top more than once.
Record a 500 Hz trace of a few first swings first; tune in simulation before the rig.

**Touch display occasionally missed at power-on.** Firmware sometimes doesn't set up the panel
(no DSI node); warm reboots don't recover it, a power-cycle does. The display guard logs power +
display each boot (`journalctl -b -u pendulum-display-guard`). Explicit overlay and the boot
splash both caused black-screen boots on this Pi — disabled. Next: investigate a panel power reset.
