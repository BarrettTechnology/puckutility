# CAN adapters

Two USB adapters are used with P4 pucks. Either one works for the A/B test.

| | PEAK PCAN-USB | CANable (P4 dev board, or standalone) |
|---|---|---|
| Firmware | PEAK's | candleLight (gs_usb protocol), multiboard build |
| Linux driver | `peak_usb` (mainline kernel) | `gs_usb` (mainline kernel) |
| USB ID (`lsusb`) | `0c72:000c` (PCAN-USB), `0c72:0012` (PCAN-USB FD) | `1d50:606f`; `0483:df11` while in its bootloader |
| CAN FD | PCAN-USB: no. PCAN-USB FD: yes | yes |
| Bus-off auto restart (`restart-ms`) | supported | **rejected** ("Device doesn't support restart from Bus Off") |
| Windows | PEAK driver + PCAN-Basic API (`PCANBasic.dll`) | no driver: Windows binds WinUSB itself on first plug-in |
| Windows port (`--can`) | `0` = `PCAN_USBBUS1`, `1` = `PCAN_USBBUS2`, ... | `0` = first CANable, `1` = second, ... |

On Windows a CANable takes precedence: when one is plugged in, `--can N`
opens the Nth CANable. Unplug it to use a PCAN.

## Which one do I have?

Linux:
```
$ lsusb | grep -i -e 0c72 -e 1d50 -e 0483:df11
$ readlink -f /sys/class/net/can0/device/driver     # .../peak_usb or .../gs_usb
$ set-can-iface.sh list                             # installed with the CAN support package
IFACE      ADAPTER      STATE   USB PATH
can0       CANable      up      1-2:1.0
$ ip -d link show can0                              # bit timing, "fd on" or not, state
```
Windows: Device Manager. Look at the device's Hardware IDs (Properties >
Details) for `VID_0C72` (PEAK) or `VID_1D50&PID_606F` (CANable).

## Bringing the link up (Linux)

The CAN support package (see `install.md`) brings every adapter up by itself
when it is plugged in. It tries three settings in order:
1. CAN FD, 1 Mbit/s nominal and 5 Mbit/s data, sample points 68.7 %, SJW 50 / 10.
   These are the bench settings. The SJWs are counted in time quanta of the
   CANable's 160 MHz clock.
2. CAN FD at 1M/5M on the kernel's default timing, for an FD adapter whose
   clock rejects those SJWs.
3. Classic CAN at 1 Mbit/s, for adapters without FD (PCAN-USB).

After that it sets `restart-ms 100` where the adapter allows it (PCAN), sets
`txqueuelen 1000`, and brings the link up. The same order runs from
`reset_can.sh can0` (recover a wedged link) and
`set-can-iface.sh <canable|peak> <0..3>` (move an adapter to a chosen canN).

By hand, classic:
```
$ sudo ip link set can0 down
$ sudo ip link set can0 type can bitrate 1000000 restart-ms 100   # drop restart-ms on a CANable
$ sudo ip link set can0 txqueuelen 1000
$ sudo ip link set can0 up
```
Check with `ip -d link show can0`. You want `bitrate 1000000` and
`state ERROR-ACTIVE`. `BUS-OFF` means no other node acknowledged: no
power, no termination, or the wrong bitrate. On a CANable, `BUS-OFF` stays
until the link goes down and up again (`reset_can.sh can0`), because it
cannot restart by itself.

## Why the A/B test is classic CAN at 1 Mbit/s

4.3.5 speaks classic CAN only. 5.0.5 can use CAN FD, but the comparison
needs the same frames on the same bus for both firmwares. The tools send
classic frames unless asked for FD. A link brought up with `fd on` still
carries classic frames, at the 1 Mbit/s nominal rate, so the automatic
bring-up above is fine as it is. Do not pass `--fd` to any command in the
A/B.

A 1 Mbit/s bus needs 120 ohm termination at both ends, about 60 ohm
between CAN_H and CAN_L with the power off.

## Updating a CANable's firmware

```
$ puckutility flash-canable          # the bundled candleLight multiboard build
```
This puts the adapter into its DFU bootloader (`0483:df11`) and writes the
image.
- Linux: it uses `dfu-util` and the udev rule from the CANable support
  package. It needs no sudo.
- Windows: it uses STM32CubeProgrammer's `STM32_Programmer_CLI`, which
  installs STM's DFU driver. The Windows installer offers to fetch it.

A CANable that does not show up as `1d50:606f` at all (no `gs_usb` link,
nothing in Device Manager) is usually stuck in DFU or has other firmware.
Flash it.
