#!/usr/bin/env python3
"""
test_fd.py  –  Candlelight WinUSB driver, pure-Python implementation
               for CANable 2.5 (or any device running Candlelight firmware).

Demonstrates:
  - Classic CAN  send/receive  @ 1 Mbps
  - CAN FD       send/receive  @ 1 Mbps nominal / 5 Mbps data

Requirements (Windows, Python 3.12):
  pip install pyusb
  libusb-1.0.dll v1.0.29  in the script directory or on PATH
  CANable 2.5 with Candlelight firmware and WinUSB driver installed

Why not python-can gs_usb?
  python-can 4.6.1 gs_usb interface hard-codes CAN 2.0 (GS_CAN_MODE_FD is
  commented-out in gs_usb 0.3.1).  This script talks the Elmüsoft protocol
  directly over WinUSB via libusb, which fully supports CAN FD.

Demo mode:
  Set LOOPBACK = True to receive your own transmissions on the same adapter
  (useful when no second node is on the bus).
"""

import struct
import threading
import time
import queue

import usb.core
import usb.util
import usb.backend.libusb1

# ── Demo knob ────────────────────────────────────────────────────────────────
LOOPBACK = False   # True → adapter echoes TX back as RX (no second node needed)

# ── USB identifiers ──────────────────────────────────────────────────────────
VID            = 0x1D50   # OpenMoko / candleLight standard VID
PID            = 0x606F   # candleLight standard PID
IFACE_CANDLE   = 0        # Interface 0 = Candlelight;  1 = DFU
EP_OUT         = 0x02
EP_IN          = 0x81
EP_TIMEOUT_MS  = 500
BULK_READ_SIZE = 128      # RX_FIFO_BUF_SIZE in Candlelight.h

# ── USB request codes (eUsbRequest in Candlelight_def.h) ─────────────────────
REQ_SET_BITTIMING    = 1
REQ_SET_MODE         = 2
REQ_GET_CAPABILITIES = 4
REQ_GET_VERSION      = 5
REQ_SET_BITTIMING_FD = 10
REQ_GET_CAPS_FD      = 11
REQ_GET_LAST_ERROR   = 22   # ELM_ReqGetLastError

# bmRequestType values (Vendor | Interface)
RT_OUT = 0x41   # Host → Device
RT_IN  = 0xC1   # Device → Host

# ── Device mode flags (eDeviceFlags) ─────────────────────────────────────────
FLAG_LOOPBACK        = 0x00002
FLAG_CAN_FD          = 0x00100
FLAG_BITTIMING_FD    = 0x00400   # "FD data bitrate has been configured"
FLAG_PROTOCOL_ELMUE  = 0x04000
FLAG_DISABLE_TX_ECHO = 0x08000

# ── Device modes (eDeviceMode) ───────────────────────────────────────────────
MODE_RESET = 0
MODE_START = 1

# ── Frame flags (eFrameFlags) ────────────────────────────────────────────────
FRM_FDF = 0x02
FRM_BRS = 0x04
FRM_ESI = 0x08

# ── CAN-ID flag bits packed into the 32-bit can_id field ─────────────────────
CAN_FLAG_RTR = 0x40000000
CAN_FLAG_EXT = 0x80000000
CAN_MASK_11  = 0x000007FF
CAN_MASK_29  = 0x1FFFFFFF

# ── Message types (eMessageType) ─────────────────────────────────────────────
MSG_TX_FRAME = 10
MSG_TX_ECHO  = 11
MSG_RX_FRAME = 12
MSG_ERROR    = 13
MSG_STRING   = 14
MSG_BUSLOAD  = 15

FBK_SUCCESS = 2   # eFeedback::FBK_Success

# ── Struct layouts (1-byte packing = no padding, little-endian) ──────────────

# kBitTiming { prop(4) seg1(4) seg2(4) sjw(4) brp(4) }
ST_BITTIMING = struct.Struct('<IIIII')    # 20 bytes

# kDeviceMode { mode(4) flags(4) }
ST_MODE = struct.Struct('<II')            # 8 bytes

# kCapabilityClassic { feature(4) fclk(4) kTimeMinMax(8×uint32=32) }
ST_CAPS = struct.Struct('<II8I')          # 40 bytes

# kCapabilityFD { feature(4) fclk(4) kTimeMinMax_nom(32) kTimeMinMax_data(32) }
ST_CAPS_FD = struct.Struct('<II8I8I')     # 72 bytes

# kDeviceVersion { reserved×3(1ea) icount(1) sw_ver(4) hw_ver(4) }
ST_VERSION = struct.Struct('<BBBBII')     # 12 bytes

# kTxFrameElmue { size(1) msg_type(1) flags(1) can_id(4) marker(1) }  + data[]
#   All Elmüsoft structs are 1-byte aligned → no padding before the uint32
ST_TX_HDR = struct.Struct('<BBBIB')       # 8 bytes

# kRxFrameElmue prefix (without optional timestamp) { size(1) msg_type(1) flags(1) can_id(4) }
ST_RX_HDR = struct.Struct('<BBBI')        # 7 bytes   data = raw[7 : raw[0]]


# ─────────────────────────────────────────────────────────────────────────────
def _calc_bit_timing(fclk: int, bitrate: int, sample_point: float = 0.75):
    """
    Return (brp, seg1, seg2, sjw) for the requested bitrate.

    Uses the same timing equation as Candlelight.cpp:
        baudrate = fclk / brp / (1 + seg1 + seg2)   with prop = 0

    Searches BRP 1..512 and picks the solution whose actual baudrate and
    sample-point are closest to the targets.  Raises ValueError if none found.
    """
    best, best_err = None, float('inf')
    for brp in range(1, 513):
        total_tq = fclk // (brp * bitrate)
        if total_tq < 3 or total_tq > 257:
            continue
        seg1 = max(1, round(sample_point * total_tq) - 1)
        seg2 = total_tq - 1 - seg1
        if seg2 < 1:
            seg2, seg1 = 1, total_tq - 2
        if seg1 < 1:
            continue
        actual_baud = fclk // (brp * (1 + seg1 + seg2))
        actual_sp   = (1 + seg1) / (1 + seg1 + seg2)
        err = abs(actual_baud - bitrate) / bitrate + abs(actual_sp - sample_point)
        if err < best_err:
            best_err, best = err, (brp, seg1, seg2, min(seg1, seg2))
    if best is None:
        raise ValueError(f"No valid bit timing for {bitrate} bps at fclk={fclk} Hz")
    return best


# ─────────────────────────────────────────────────────────────────────────────
class CanMessage:
    """Lightweight CAN / CAN FD frame container."""

    def __init__(self, arbitration_id: int, data: bytes,
                 is_extended_id: bool = False, is_remote_frame: bool = False,
                 is_fd: bool = False, bitrate_switch: bool = False):
        self.arbitration_id  = arbitration_id
        self.data            = bytes(data)
        self.is_extended_id  = is_extended_id
        self.is_remote_frame = is_remote_frame
        self.is_fd           = is_fd
        self.bitrate_switch  = bitrate_switch

    def __repr__(self):
        id_str = (f"0x{self.arbitration_id:08X}" if self.is_extended_id
                  else f"0x{self.arbitration_id:03X}")
        fd_tag = (" FD+BRS" if (self.is_fd and self.bitrate_switch)
                  else " FD" if self.is_fd else "")
        rtr_tag = " RTR" if self.is_remote_frame else ""
        return (f"CanMessage(id={id_str}{fd_tag}{rtr_tag}, "
                f"dlc={len(self.data)}, data={self.data.hex(' ')})")


# ─────────────────────────────────────────────────────────────────────────────
class Candlelight:
    """
    Pure-Python driver for the CANable 2.5 Candlelight firmware over WinUSB.

    Implements the Elmüsoft USB protocol as documented in:
      SampleApplication C++/Source/Candlelight/{Candlelight.cpp,.h,_def.h}
    from https://github.com/Elmue/CANable-2.5-firmware-Slcan-and-Candlelight

    Uses pyusb (libusb-1.0.dll backend) for all USB I/O.
    """

    def __init__(self, vid: int = VID, pid: int = PID, channel: int = 0):
        self._vid     = vid
        self._pid     = pid
        self._channel = channel
        self._dev     = None
        self._rx_q    = queue.Queue()
        self._running = False
        self._thread  = None
        self._marker  = 0          # rolling TX echo marker (0–255)
        self.fclk_nom  = None      # Hz, from GS_ReqGetCapabilities
        self.fclk_data = None      # Hz, from GS_ReqGetCapabilitiesFD
        self.caps_feat  = 0
        self.caps_fd_feat = 0

    # ── Context manager ───────────────────────────────────────────────────────
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ── Connection ────────────────────────────────────────────────────────────
    def open(self):
        """Find device via libusb, reset it, and read capabilities."""
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            raise IOError("libusb-1.0.dll not found – ensure it is on PATH or in the script directory")

        self._dev = usb.core.find(idVendor=self._vid, idProduct=self._pid,
                                  backend=backend)
        if self._dev is None:
            raise IOError(f"Device {self._vid:04X}:{self._pid:04X} not found")

        # Set configuration (required by pyusb; WinUSB ignores it)
        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass

        usb.util.claim_interface(self._dev, IFACE_CANDLE)

        # Reset the adapter to a known state
        self._ctrl_out(REQ_SET_MODE, ST_MODE.pack(MODE_RESET, 0))

        # Read firmware version
        raw = self._ctrl_in(REQ_GET_VERSION, ST_VERSION.size)
        _, _, _, icount, sw_ver, hw_ver = ST_VERSION.unpack(raw)
        print(f"Firmware version  sw=0x{sw_ver:08X}  hw=0x{hw_ver:08X}  ch={icount}")

        # Read classic-CAN capabilities (provides fclk_can for nominal timing)
        raw = self._ctrl_in(REQ_GET_CAPABILITIES, ST_CAPS.size)
        feat, fclk, *_ = ST_CAPS.unpack(raw)
        self.fclk_nom  = fclk
        self.caps_feat = feat
        print(f"fclk_nominal = {fclk/1e6:.1f} MHz  features = 0x{feat:04X}")

        # Read FD capabilities (provides fclk_can for data-phase timing)
        raw = self._ctrl_in(REQ_GET_CAPS_FD, ST_CAPS_FD.size)
        feat_fd, fclk_fd, *_ = ST_CAPS_FD.unpack(raw)
        self.fclk_data    = fclk_fd
        self.caps_fd_feat = feat_fd
        print(f"fclk_data    = {fclk_fd/1e6:.1f} MHz  features_fd = 0x{feat_fd:04X}")

        # Start background receive thread
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, name="candlelight-rx",
                                        daemon=True)
        self._thread.start()

    def close(self):
        """Stop receive thread, reset adapter, release interface."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._dev:
            try:
                self._ctrl_out(REQ_SET_MODE, ST_MODE.pack(MODE_RESET, 0))
            except Exception:
                pass
            usb.util.release_interface(self._dev, IFACE_CANDLE)
            usb.util.dispose_resources(self._dev)
            self._dev = None
        print("Closed.")

    # ── Configuration ─────────────────────────────────────────────────────────
    def set_bitrate(self, bitrate: int, fd_data: bool = False,
                    sample_point: float = 0.75):
        """
        Configure nominal (fd_data=False) or FD data-phase (fd_data=True) bitrate.
        Must be called before start().
        """
        fclk = self.fclk_data if fd_data else self.fclk_nom
        brp, seg1, seg2, sjw = _calc_bit_timing(fclk, bitrate, sample_point)
        actual_baud = fclk // (brp * (1 + seg1 + seg2))
        actual_sp   = (1 + seg1) / (1 + seg1 + seg2)
        tag = "FD data" if fd_data else "nominal"
        print(f"  {tag}: {actual_baud/1e6:.3f} Mbps  SP={actual_sp*100:.1f}%  "
              f"brp={brp}  seg1={seg1}  seg2={seg2}  sjw={sjw}")
        data = ST_BITTIMING.pack(0, seg1, seg2, sjw, brp)   # prop = 0
        req  = REQ_SET_BITTIMING_FD if fd_data else REQ_SET_BITTIMING
        self._ctrl_out(req, data)

    def start(self, fd: bool = False, loopback: bool = False):
        """
        Start the CAN bus.
          fd       – True  → enable CAN FD (requires set_bitrate() for both phases)
          loopback – True  → TX frames loop back to RX on the same adapter
        """
        flags = FLAG_PROTOCOL_ELMUE | FLAG_DISABLE_TX_ECHO
        if fd:
            flags |= FLAG_CAN_FD | FLAG_BITTIMING_FD
        if loopback:
            flags |= FLAG_LOOPBACK
        self._ctrl_out(REQ_SET_MODE, ST_MODE.pack(MODE_START, flags))
        mode_str = "CAN FD" if fd else "Classic CAN"
        loop_str = " [loopback]" if loopback else ""
        print(f"  Started  {mode_str}{loop_str}")

    def reset(self):
        """Return adapter to reset (bus-off) state so mode can be changed."""
        self._ctrl_out(REQ_SET_MODE, ST_MODE.pack(MODE_RESET, 0))
        # Drain any queued RX messages
        while not self._rx_q.empty():
            try:
                self._rx_q.get_nowait()
            except queue.Empty:
                break

    # ── I/O ───────────────────────────────────────────────────────────────────
    def send(self, msg: CanMessage):
        """Transmit a CAN or CAN FD frame via bulk OUT endpoint."""
        arb = msg.arbitration_id
        can_id = arb & (CAN_MASK_29 if msg.is_extended_id else CAN_MASK_11)
        if msg.is_extended_id:
            can_id |= CAN_FLAG_EXT
        if msg.is_remote_frame:
            can_id |= CAN_FLAG_RTR

        flags = 0
        if msg.is_fd:
            flags |= FRM_FDF
        if msg.bitrate_switch:
            flags |= FRM_BRS

        marker = self._marker
        self._marker = (self._marker + 1) & 0xFF

        total = ST_TX_HDR.size + len(msg.data)   # 8 + N bytes
        hdr   = ST_TX_HDR.pack(total, MSG_TX_FRAME, flags, can_id, marker)
        self._dev.write(EP_OUT, hdr + msg.data, timeout=EP_TIMEOUT_MS)

    def recv(self, timeout: float = 1.0) -> CanMessage | None:
        """Return the next received frame, or None on timeout."""
        try:
            return self._rx_q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Internal ──────────────────────────────────────────────────────────────
    def _ctrl_out(self, request: int, data: bytes):
        """Vendor control transfer Host→Device, then check Elmüsoft feedback."""
        self._dev.ctrl_transfer(RT_OUT, request,
                                wValue=self._channel, wIndex=IFACE_CANDLE,
                                data_or_wLength=data, timeout=EP_TIMEOUT_MS)
        fb = self._get_feedback()
        if fb != FBK_SUCCESS:
            raise IOError(f"Request {request} rejected by firmware: "
                          f"feedback=0x{fb:02X} ({fb!r})")

    def _ctrl_in(self, request: int, length: int) -> bytes:
        """Vendor control transfer Device→Host, return response bytes."""
        return bytes(self._dev.ctrl_transfer(RT_IN, request,
                                             wValue=self._channel,
                                             wIndex=IFACE_CANDLE,
                                             data_or_wLength=length,
                                             timeout=EP_TIMEOUT_MS))

    def _get_feedback(self) -> int:
        """Read the single-byte Elmüsoft feedback code after every command."""
        raw = self._dev.ctrl_transfer(RT_IN, REQ_GET_LAST_ERROR,
                                      wValue=self._channel,
                                      wIndex=IFACE_CANDLE,
                                      data_or_wLength=1,
                                      timeout=EP_TIMEOUT_MS)
        return raw[0]

    def _rx_loop(self):
        """
        Background thread: bulk-read EP_IN and parse Elmüsoft frames.

        Frame layout (no timestamp, Elmüsoft protocol):
          Byte 0     : total size (header + data)
          Byte 1     : msg_type  (MSG_RX_FRAME = 12)
          Byte 2     : flags     (FRM_FDF, FRM_BRS, …)
          Bytes 3–6  : can_id    (uint32 LE, with CAN_FLAG_EXT / CAN_FLAG_RTR)
          Bytes 7…   : data payload  (length = raw[0] - 7)
        """
        while self._running:
            try:
                raw = bytes(self._dev.read(EP_IN, BULK_READ_SIZE,
                                           timeout=EP_TIMEOUT_MS))
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:
                if self._running:
                    print(f"[RX] USB error: {exc}")
                break

            if len(raw) < 2:
                continue

            msg_type = raw[1]

            if msg_type == MSG_RX_FRAME:
                if len(raw) < ST_RX_HDR.size:
                    continue
                size, _, flags, can_id = ST_RX_HDR.unpack(raw[:ST_RX_HDR.size])
                data   = raw[ST_RX_HDR.size : size]
                is_ext = bool(can_id & CAN_FLAG_EXT)
                is_rtr = bool(can_id & CAN_FLAG_RTR)
                arb_id = can_id & (CAN_MASK_29 if is_ext else CAN_MASK_11)
                self._rx_q.put(CanMessage(
                    arbitration_id  = arb_id,
                    data            = data,
                    is_extended_id  = is_ext,
                    is_remote_frame = is_rtr,
                    is_fd           = bool(flags & FRM_FDF),
                    bitrate_switch  = bool(flags & FRM_BRS),
                ))

            elif msg_type == MSG_TX_ECHO:
                pass  # TX echo disabled via FLAG_DISABLE_TX_ECHO; ignore if seen

            elif msg_type == MSG_STRING:
                if len(raw) > 2:
                    print(f"[FW] {raw[2:].decode('ascii', errors='replace').rstrip()}")

            elif msg_type == MSG_ERROR:
                if len(raw) >= 6:
                    err_id = struct.unpack_from('<I', raw, 2)[0]
                    print(f"[ERR] err_id=0x{err_id:04X}")


# ─────────────────────────────────────────────────────────────────────────────
def demo_classic_can(bus: Candlelight, loopback: bool = False):
    print("\n── Classic CAN @ 1 Mbps " + "─" * 48)
    bus.set_bitrate(1_000_000)
    bus.start(fd=False, loopback=loopback)

    payload = bytes([0x40,0x41,0x60,0x00])
    tx = CanMessage(0x67F, payload)
    print(f"TX: {tx}")
    bus.send(tx)

    rx = bus.recv(timeout=2.0)
    print(f"RX: {rx}" if rx else "RX: (timeout – no frame received)")


def demo_can_fd(bus: Candlelight, loopback: bool = False):
    print("\n── CAN FD @ 1 Mbps nominal / 5 Mbps data " + "─" * 31)
    bus.set_bitrate(1_000_000, fd_data=False)
    bus.set_bitrate(5_000_000, fd_data=True)
    bus.start(fd=True, loopback=loopback)

    payload = bytes([0x40,0x41,0x60,0x00])
    tx = CanMessage(0x67F, payload, is_fd=True, bitrate_switch=True)
    print(f"TX: {tx}")
    bus.send(tx)

    rx = bus.recv(timeout=2.0)
    print(f"RX: {rx}" if rx else "RX: (timeout – no frame received)")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with Candlelight() as bus:
        demo_classic_can(bus, loopback=LOOPBACK)

        bus.reset()
        time.sleep(0.1)

        demo_can_fd(bus, loopback=LOOPBACK)
