# CAN FD on Windows with CANable 2.5 / Candlelight Firmware

## Background

The standard Python CAN stack (`python-can` + `gs_usb`) does **not** support CAN FD
as of the versions tested:

| Package | Version | CAN FD via gs_usb? |
|---|---|---|
| python-can | 4.6.1 | No – `GsUsbBus` hard-codes `CAN_20` protocol |
| gs_usb | 0.3.1 | No – `GS_CAN_MODE_FD` exists in constants but is commented out |

`python-can 4.6.0` added FD support for the **slcan** interface (CANable 2.0
implementation), but that requires flashing slcan firmware and using a COM port
instead of WinUSB/libusb.

## Solution

The CANable 2.5 [Candlelight firmware by Elmue](https://github.com/Elmue/CANable-2.5-firmware-Slcan-and-Candlelight)
exposes a well-documented USB protocol (the *Elmüsoft protocol*) over WinUSB.
`candlelight_bus.py` implements that protocol directly via `pyusb` and wraps it
as a `python-can` `BusABC` — no compilation required, works with any `python-can`
consumer (canopen, etc.).  `test_fd.py` is a demo script that imports
`CandlelightBus` from `candlelight_bus.py` and uses standard `can.Message` objects.

## Requirements

| Item | Version |
|---|---|
| Python | 3.12+ |
| pyusb | any recent (e.g. 1.3.1) |
| python-can | any recent (e.g. 4.6.1) |
| libusb-1.0.dll | 1.0.29 — place in script directory or on `PATH` |
| Firmware | CANable 2.5 with Candlelight firmware |
| Windows driver | WinUSB (install via [Zadig](https://zadig.akeo.ie/)) |

```
pip install pyusb python-can
```

## Quick Start

```
python test_fd.py
```

Expected output (with a second node or `LOOPBACK = True`):

```
── Classic CAN @ 1 Mbps ────────────────────────────────────────────────
TX: Timestamp: 1700000000.000  ID: 067f    S Len: 004 Data: 40 41 60 00
RX: Timestamp: 1700000000.002  ID: 067f    S Len: 004 Data: 40 41 60 00

── CAN FD @ 1 Mbps nominal / 5 Mbps data ──────────────────────────────
TX: Timestamp: 1700000000.101  ID: 067f    S  FD BRS Len: 004 Data: 40 41 60 00
RX: Timestamp: 1700000000.103  ID: 067f    S  FD BRS Len: 004 Data: 40 41 60 00
```

Driver diagnostics (firmware version, fclk, bit timing) are emitted via the
standard Python `logging` module at `INFO` level and are silent by default.
To enable them: `logging.basicConfig(level=logging.INFO)`

Set `LOOPBACK = True` at the top of `test_fd.py` to receive your own
transmissions on the same adapter (no second node needed for testing).

---

## How It Works

### USB Access

`pyusb` with the `libusb1` backend calls into `libusb-1.0.dll`, which uses the
Windows WinUSB API under the hood.  Because libusb 1.0.27+ supports WinUSB
natively, this works without any separate driver layer.

The device is found by VID/PID:

```python
usb.core.find(idVendor=0x1D50, idProduct=0x606F)
```

(`0x1D50:0x606F` is the standard candleLight VID/PID, confirmed in Elmue's
C++ source from the device path `VID_1D50&PID_606F`.)

Two bulk endpoints are used:

| Endpoint | Direction | Use |
|---|---|---|
| `0x02` | OUT | transmit CAN frames |
| `0x81` | IN | receive CAN frames and firmware messages |

### USB Control Transfers

All configuration commands use USB vendor control transfers on interface 0.

```
bmRequestType = 0x41  (Vendor | Interface | Host→Device)   for commands
bmRequestType = 0xC1  (Vendor | Interface | Device→Host)   for queries
wValue        = channel number (0 for single-channel devices)
wIndex        = interface number (0)
```

After every outgoing command the driver reads one feedback byte via
`ELM_ReqGetLastError` (request 22).  A value of `2` (`FBK_Success`) confirms
success; anything else raises an exception.

### Startup Sequence

Everything happens in `CandlelightBus.__init__`:

```
CandlelightBus(channel=0, bitrate=1_000_000, fd=True, data_bitrate=5_000_000)
  │
  ├─ ctrl_out  REQ_SET_MODE          (reset)           → confirm feedback
  ├─ ctrl_in   REQ_GET_VERSION                         → log fw version
  ├─ ctrl_in   REQ_GET_CAPABILITIES                    → read fclk_nominal
  ├─ ctrl_in   REQ_GET_CAPS_FD                         → read fclk_data
  ├─ ctrl_out  REQ_SET_BITTIMING     (nominal timing)  → confirm feedback
  ├─ ctrl_out  REQ_SET_BITTIMING_FD  (data timing)     → confirm feedback (FD only)
  └─ ctrl_out  REQ_SET_MODE          (start + flags)   → confirm feedback
```

Receiving is handled by python-can's `BusABC` machinery via `_recv_internal()`.

### Bit Timing

The firmware uses this equation (matching `Candlelight.cpp`):

```
baudrate = fclk / brp / (1 + seg1 + seg2)    (prop = 0)
```

`_calc_bit_timing()` searches BRP 1–512 and selects the combination whose
actual baudrate and sample point are closest to the requested values.

Example results at `fclk = 160 MHz`:

| Target | BRP | seg1 | seg2 | sjw | Actual | SP |
|---|---|---|---|---|---|---|
| 1 Mbps nominal | 2 | 59 | 20 | 20 | 1.000 Mbps | 75.0% |
| 5 Mbps data    | 2 | 11 | 4  | 4  | 5.000 Mbps | 75.0% |

`test_fd.py` uses `sample_point=0.75` (75%) for both phases.

### `kBitTiming` Struct (20 bytes, little-endian)

```
Offset  Size  Field
0       4     prop   (set to 0)
4       4     seg1
8       4     seg2
12      4     sjw
16      4     brp
```

Python: `struct.Struct('<IIIII')`

### `kDeviceMode` Struct (8 bytes, little-endian)

```
Offset  Size  Field
0       4     mode   (0 = reset, 1 = start)
4       4     flags  (eDeviceFlags bitmask)
```

Python: `struct.Struct('<II')`

Relevant `flags` bits used in `test_fd.py`:

| Flag | Value | Meaning |
|---|---|---|
| `FLAG_LOOPBACK` | `0x00002` | TX frames loop back as RX |
| `FLAG_CAN_FD` | `0x00100` | Enable CAN FD mode |
| `FLAG_BITTIMING_FD` | `0x00400` | FD data-phase bitrate has been configured |
| `FLAG_PROTOCOL_ELMUE` | `0x04000` | Use Elmüsoft framing (required) |
| `FLAG_DISABLE_TX_ECHO` | `0x08000` | Suppress TX echo messages |

For classic CAN: `FLAG_PROTOCOL_ELMUE | FLAG_DISABLE_TX_ECHO`
For CAN FD:     `FLAG_PROTOCOL_ELMUE | FLAG_DISABLE_TX_ECHO | FLAG_CAN_FD | FLAG_BITTIMING_FD`

### TX Frame Layout (Elmüsoft protocol, 1-byte packed)

All Elmüsoft structs use **1-byte alignment** — no padding between fields.
Python's `struct` module with `'<'` (little-endian, no alignment) matches this
exactly.

```
Offset  Size  Field
0       1     size      total frame size = 8 + len(data)
1       1     msg_type  MSG_TX_FRAME = 10
2       1     flags     FRM_FDF (0x02) | FRM_BRS (0x04) for CAN FD + BRS
3       4     can_id    29-bit or 11-bit ID with flag bits:
                          bit 31 = CAN_FLAG_EXT  (0x80000000)
                          bit 30 = CAN_FLAG_RTR  (0x40000000)
7       1     marker    rolling TX echo marker (0–255)
8       N     data      CAN payload (0–8 bytes classic, 0–64 bytes FD)
```

Python: `struct.Struct('<BBBIB')` for the 8-byte header, data appended after.

### RX Frame Layout (Elmüsoft protocol, without timestamp)

Timestamps are disabled by not setting `FLAG_TIMESTAMP` in Start flags.
Without timestamps, the data payload begins immediately after the 7-byte header.

```
Offset  Size  Field
0       1     size      total frame size = 7 + len(data)
1       1     msg_type  MSG_RX_FRAME = 12
2       1     flags     FRM_FDF (0x02) | FRM_BRS (0x04) | FRM_ESI (0x08)
3       4     can_id    ID with CAN_FLAG_EXT / CAN_FLAG_RTR in upper bits
7       N     data      payload, length = raw[0] - 7
```

Python: `struct.Struct('<BBBI')` for the 7-byte header.

### Other Message Types on EP_IN

The firmware can also send these message types on the bulk IN endpoint:

| Value | Name | Description |
|---|---|---|
| 11 | `MSG_TX_ECHO` | Confirmation of a transmitted frame (suppressed by `FLAG_DISABLE_TX_ECHO`) |
| 13 | `MSG_ERROR` | CAN bus error with `err_id` bitmask and 8 error-detail bytes |
| 14 | `MSG_STRING` | ASCII debug string from firmware |
| 15 | `MSG_BUSLOAD` | Single-byte bus load percentage |

### Background Receive Thread

The C++ driver notes that WinUSB has no internal RX buffer — packets are lost
if the host does not read fast enough.  `CandlelightBus` implements
`_recv_internal()`, and python-can's `BusABC` calls it from a background
`Notifier` thread (or directly from `bus.recv()`).  Each bulk read on `EP_IN`
uses a 500 ms timeout; USB timeout errors are silently retried.

---

## Protocol Reference

Source files (C++, in the firmware repository):

```
SampleApplication C++/Source/
  Candlelight/
    Candlelight.h        — class Candlelight: public API and structs
    Candlelight_def.h    — all enums, constants, and packed structs
    Candlelight.cpp      — implementation: Open, SetBitrate, Start, Send, Recv
  CANableDemo.cpp        — example call sequences for classic CAN and CAN FD
```

Firmware repository:
<https://github.com/Elmue/CANable-2.5-firmware-Slcan-and-Candlelight>

---

## Verifying the VID/PID

If the device is not found, list all USB devices to check the actual VID/PID:

```python
import usb.core
for d in usb.core.find(find_all=True):
    print(f"{d.idVendor:04X}:{d.idProduct:04X}  {d.manufacturer}  {d.product}")
```

Then update `VID` and `PID` at the top of `candlelight_bus.py`, or pass them
as `vid=` / `pid=` keyword arguments to `CandlelightBus()`.

## Switching Between Classic CAN and CAN FD

Each `CandlelightBus` instance resets the adapter on construction and starts it
with the requested mode.  To switch modes, close the current bus and open a new
one.  `test_fd.py` does this implicitly — `demo_classic_can()` and
`demo_can_fd()` each open and close their own `CandlelightBus` via a `with`
block, so the adapter is cleanly reset between the two demos.
