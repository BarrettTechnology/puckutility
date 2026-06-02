# CANopen over CAN FD – Architecture and Implementation

## Overview

`testp4.py` runs CANopen motion-control tests on a Barrett Technology Puck
motor over a CAN bus.  It supports two platforms:

- **Linux** — SocketCAN via `python-can` (existing, unchanged)
- **Windows** — CANable 2.5 with Candlelight firmware via a custom
  `python-can` bus driver (`candlelight_bus.py`)

A single flag, `USE_FD`, switches all CANopen traffic between classic CAN
and CAN FD on both platforms identically.

---

## Why a Custom Driver?

The standard `python-can[gs_usb]` stack **does not support CAN FD**:

| Package | Version | FD support |
|---|---|---|
| python-can gs_usb interface | 4.6.1 | No — hard-codes `CAN_20` protocol |
| gs_usb Python package | 0.3.1 | No — `GS_CAN_MODE_FD` is commented out |

The CANable 2.5 [Candlelight firmware by Elmue](https://github.com/Elmue/CANable-2.5-firmware-Slcan-and-Candlelight)
implements a well-documented USB protocol (*Elmüsoft protocol*) over WinUSB.
`candlelight_bus.py` implements that protocol directly using `pyusb` with the
`libusb-1.0.dll` backend, bypassing python-can's gs_usb interface entirely.

---

## Software Stack

```
testp4.py (application)
    │
    │  CANopen protocol layer
    ▼
canopen.Network  (or FDNetwork when USE_FD=True)
    │
    │  can.Message objects — is_fd / bitrate_switch set by FDNetwork
    ▼
can.BusABC interface
    │
    ├── Linux: python-can SocketCAN bus (bustype='socketcan')
    │       fd=True, data_bitrate=5_000_000 passed to connect()
    │
    └── Windows: CandlelightBus (candlelight_bus.py)
            pyusb → libusb-1.0.dll → WinUSB → CANable 2.5
```

---

## Files

| File | Purpose |
|---|---|
| `testp4.py` | CANopen test application — platform detection, `FDNetwork`, all motion-control tests |
| `candlelight_bus.py` | `CandlelightBus(can.BusABC)` — full Candlelight/Elmüsoft USB driver |
| `test_fd.py` | Standalone send/receive demo using `candlelight_bus.py` directly |
| `README_CANDLELIGHT25.md` | Candlelight USB protocol reference |

---

## Configuration

In `testp4.py` (near the top):

```python
USE_FD          = False      # True → CAN FD for all CANopen traffic
FD_DATA_BITRATE = 5_000_000  # data-phase bitrate (bits/s)
```

The nominal bitrate is always **1 Mbps**.  The data-phase bitrate applies
only when `USE_FD = True`.

---

## How FD Framing Is Applied — Two-Layer Design

CANopen's message-building code (`canopen.Network.send_message`) creates plain
`can.Message` objects without FD flags.  Getting FD onto the wire requires
intervention at two points in the stack.  Both layers are active when
`USE_FD = True`.

### Layer 1 — FDNetwork (canopen layer)

`FDNetwork` is a minimal subclass of `canopen.Network` defined in `testp4.py`.
It overrides `send_message` to add `is_fd=True` and `bitrate_switch=True` to
every outgoing `can.Message` before handing it to the bus:

```python
class FDNetwork(canopen.Network):
    def send_message(self, can_id, data, remote=False):
        msg = can.Message(arbitration_id=can_id, data=data,
                          is_extended_id=can_id > 0x7FF,
                          is_remote_frame=remote,
                          is_fd=True, bitrate_switch=True)
        with self.send_lock:
            self.bus.send(msg)
        self.check()
```

All SDO, PDO, SYNC, and NMT traffic flows through `send_message`, so this
single override covers the entire CANopen protocol.  `send_lock` (inherited)
ensures thread safety when PDOs and SDOs transmit concurrently.  `self.check()`
(inherited) raises on bus errors, preserving the parent's error-reporting
behaviour.

`FDNetwork` is used on **both** platforms when `USE_FD = True`, providing a
consistent interface regardless of the underlying hardware.

### Layer 2 — CandlelightBus.send() (hardware layer, Windows only)

`CandlelightBus` independently forces FD+BRS on every outgoing frame when
the bus was opened with `fd=True`:

```python
# In CandlelightBus.send():
use_fd  = msg.is_fd  or self._fd
use_brs = msg.bitrate_switch or self._fd
```

This means:
- If only `CandlelightBus(fd=True)` is used (without `FDNetwork`), all frames
  still go out as FD+BRS — useful when integrating the bus with other
  python-can consumers that are not FD-aware.
- When both layers are active the flags are set twice (harmlessly).

On **Linux**, SocketCAN respects the `is_fd` and `bitrate_switch` flags set by
`FDNetwork` and transmits the frame accordingly.  No second layer is needed.

### Summary

| Condition | `FDNetwork` active? | `CandlelightBus(fd=True)`? | Frames on wire |
|---|---|---|---|
| `USE_FD=False`, Linux | No | — | Classic CAN |
| `USE_FD=True`,  Linux | Yes | — | CAN FD + BRS |
| `USE_FD=False`, Windows | No | No | Classic CAN |
| `USE_FD=True`,  Windows | Yes | Yes (redundant) | CAN FD + BRS |

---

## Connection Sequence

```python
NetworkClass = FDNetwork if USE_FD else canopen.Network

# Windows
bus     = CandlelightBus(channel=int(can_device), bitrate=1_000_000,
                         fd=USE_FD, data_bitrate=FD_DATA_BITRATE)
network = NetworkClass(bus=bus)
network.connect()          # skips can.Bus() — starts can.Notifier only

# Linux
network = NetworkClass()
network.connect(bustype='socketcan', channel=can_device,
                bitrate=1_000_000,
                fd=True, data_bitrate=FD_DATA_BITRATE)   # USE_FD=True only
```

`canopen.Network.__init__` accepts an optional `bus` argument.  When it is
set, `network.connect()` skips `can.Bus()` construction and proceeds directly
to starting the `can.Notifier`.  This is the standard canopen mechanism for
injecting a custom bus.

---

## Receive Path — can.Notifier

canopen starts a `can.Notifier` inside `network.connect()`:

```python
self.notifier = can.Notifier(self.bus, self.listeners, timeout=1)
```

The Notifier runs a background thread that calls `bus.recv()` in a loop with
a 1-second timeout.  `bus.recv()` calls `_recv_internal()`, which performs a
single USB bulk read on Windows or a SocketCAN read on Linux.  Received
`can.Message` objects are passed to `MessageListener`, which dispatches them
to the registered SDO, PDO, SYNC, and NMT handlers inside canopen.

`CandlelightBus._recv_internal()` handles non-frame USB packets internally
(TX echo, firmware strings, error reports) and returns `(None, False)` for
them so the Notifier retries.  Only `MSG_RX_FRAME` packets become
`can.Message` objects and are returned to canopen.

Because the Notifier provides the RX thread, `CandlelightBus` has **no
background thread of its own**.  This avoids the double-threading problem
that would occur if both the bus and canopen tried to read from the same
endpoint concurrently.

---

## CandlelightBus Internals

See `README_CANDLELIGHT25.md` for the full Elmüsoft USB protocol reference.
Key points relevant to the canopen integration:

**Construction** — the bus is fully ready to use after `__init__` returns:

```
__init__()
  ├── usb.core.find(VID, PID)        open USB device via libusb
  ├── REQ_SET_MODE (reset)           known state
  ├── REQ_GET_VERSION                log firmware version
  ├── REQ_GET_CAPABILITIES           read fclk_nominal
  ├── REQ_GET_CAPS_FD                read fclk_data
  ├── REQ_SET_BITTIMING              configure nominal bitrate
  ├── REQ_SET_BITTIMING_FD           configure data bitrate (fd=True only)
  ├── REQ_SET_MODE (start + flags)   start CAN bus
  └── super().__init__()             sets _is_shutdown = False
```

`super().__init__()` is called **last**, as required by `can.BusABC` — it
sets `_is_shutdown = False`, signalling that the subclass constructor
completed successfully.

**Sending** — `send()` builds an Elmüsoft TX frame and writes it to bulk
endpoint `0x02`.  Frame layout (8-byte header, 1-byte packed):

```
Byte 0     size        = 8 + len(data)
Byte 1     msg_type    = 10 (MSG_TX_FRAME)
Byte 2     flags       FRM_FDF (0x02) | FRM_BRS (0x04) when FD
Bytes 3–6  can_id      uint32 LE with CAN_FLAG_EXT / CAN_FLAG_RTR in upper bits
Byte 7     marker      rolling 0–255 echo-correlation byte
Bytes 8…   data
```

**Receiving** — `_recv_internal()` performs a single bulk read from endpoint
`0x81` with a timeout capped at `EP_TIMEOUT_MS` (500 ms).  The timeout cap
prevents a single call from blocking longer than the Notifier's 1-second
cycle, allowing clean shutdown.

**Shutdown** — `shutdown()` sends `REQ_SET_MODE (reset)`, releases the USB
interface, and calls `super().shutdown()` which sets `_is_shutdown = True`.
Called automatically by `network.disconnect()`.

---

## Bit Timing

Both platforms use the same equation (matching `Candlelight.cpp`):

```
baudrate = fclk / brp / (1 + seg1 + seg2)     prop = 0
```

`_calc_bit_timing()` in `candlelight_bus.py` searches BRP 1–512 and selects
the combination whose actual baudrate and sample point are closest to the
targets (default SP = 75%).

Example at fclk = 160 MHz (STM32G4 typical):

| Phase | Target | BRP | seg1 | seg2 | sjw | Actual | SP |
|---|---|---|---|---|---|---|---|
| Nominal | 1 Mbps | 2 | 59 | 20 | 20 | 1.000 Mbps | 75.0% |
| Data    | 5 Mbps | 2 | 11 | 4  | 4  | 5.000 Mbps | 75.0% |

The `fclk` values are read from the device at startup via
`REQ_GET_CAPABILITIES` and `REQ_GET_CAPS_FD`, so the timing calculation
adapts automatically if the firmware uses a different clock.

---

## Running

**Linux:**
```
python testp4.py can0 1
```

**Windows:**
```
python testp4.py 0 1
```

The first argument is the CAN interface — a SocketCAN device name on Linux
(`can0`, `can1`, …) or a USB adapter index on Windows (`0` for the first
Candlelight device found).  The second argument is the CANopen node ID.

**Required files on the Windows machine:**

```
testp4.py
candlelight_bus.py
../puck4.eds         (one directory up, as referenced in the script)
libusb-1.0.dll       (v1.0.29, on PATH or in the script directory)
```

**Python packages (Windows):**
```
pip install canopen pyusb
```
