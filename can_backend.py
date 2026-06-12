"""Unified CAN adapter factory with runtime auto-detection.

Single entry point (`make_network`) used by both the GUI and CLI. On Windows
the adapter is chosen automatically: if a CandleLight CANable is connected
(USB VID 0x1D50 / PID 0x606F) we use the in-tree CandlelightBus driver;
otherwise we fall back to the existing PCAN path. On Linux both adapter
types appear as SocketCAN interfaces (peak-pcan / gs_usb kernel drivers),
so the dispatch is moot and we keep the SocketCAN call. macOS keeps the
existing PCAN path.

Detection is cheap (a USB enumeration with libusb-package's bundled
backend), so make_network probes every time it's called. If you plug in a
CandleLight after starting the app, the next CAN operation will pick it up
without a restart.
"""

import platform
import canopen


CANDLELIGHT_VID = 0x1D50
CANDLELIGHT_PID = 0x606F

try:
    from sandbox.candlelight_bus import CandlelightBus
    _HAS_CANDLELIGHT = True
except ImportError:
    _HAS_CANDLELIGHT = False


def _candlelight_present():
    """Return True if a CandleLight CANable is enumerated on USB. Returns
    False (rather than raising) for any error so callers can fall back to
    PCAN whenever detection is inconclusive."""
    if not _HAS_CANDLELIGHT:
        return False
    try:
        import usb.core
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            backend = None
        dev = usb.core.find(idVendor=CANDLELIGHT_VID,
                            idProduct=CANDLELIGHT_PID,
                            backend=backend)
        return dev is not None
    except Exception:
        return False


def pcan_channel(can_device):
    """Map a CAN port selector to a PCAN channel name. Accepts a bare bus
    index (e.g. '0' -> PCAN_USBBUS1) or a full 'PCAN_USBBUSn' string passed
    verbatim."""
    text = str(can_device).strip()
    if text.upper().startswith('PCAN_'):
        return text.upper()
    try:
        return 'PCAN_USBBUS' + str(int(text) + 1)
    except ValueError:
        raise ValueError(
            f"invalid PCAN device {can_device!r}: expected a bus index "
            f"(e.g. 0 for PCAN_USBBUS1) or a PCAN_USBBUSn name")


# SDO abort codes whose appearance almost always means another CANopen master
# is active on the same bus at the same time — a second copy of PuckUtility or
# PuckTuner left open, both apps connected at once, or a tool flooding the bus
# with SYNC/PDO traffic.  When two clients transmit at once their CAN frames
# interleave and corrupt each other's multi-frame SDO sequence, so the puck
# rejects the malformed request with one of these protocol-level aborts.
#
# These are distinct from object-level aborts (e.g. 0x06020000 "object does not
# exist", 0x06010002 "write to a read-only object") which point at the request
# itself, not at bus contention — those are deliberately excluded here.
SDO_CONTENTION_CODES = {
    0x05040001: "client/server command specifier not valid or unknown",
    0x05030000: "toggle bit not alternated",
    0x05040000: "SDO protocol timed out",
    0x05040002: "invalid block size",
    0x05040003: "invalid sequence number",
}


def sdo_contention_message(exc):
    """If `exc` is an SDO abort whose code indicates competing SDO/PDO traffic
    on the bus, return a clear, user-facing explanation; otherwise return None.

    Accepts a canopen SdoAbortedError (anything with an integer ``.code``) or a
    bare integer abort code.  Returning None lets callers fall through to their
    normal generic error message for ordinary, non-contention aborts.
    """
    code = getattr(exc, "code", exc)
    if not isinstance(code, int) or code not in SDO_CONTENTION_CODES:
        return None
    return (
        "SDO abort 0x{:08X} — likely CAN bus contention.\n"
        "Close any other PuckUtility/PuckTuner window using this bus and retry."
    ).format(code)


def make_network(can_device, bitrate=1_000_000):
    """Create and connect a canopen.Network. The adapter is auto-selected:
    CandleLight if present on USB, otherwise PCAN (Windows/macOS) or
    SocketCAN (Linux).

    `can_device` is the channel selector for the PCAN/SocketCAN paths. For
    CandleLight, integer values select which USB instance to open when
    multiple adapters are connected (0 = first found).
    """
    system = platform.system()

    if system == "Windows" and _candlelight_present():
        try:
            usb_index = int(str(can_device).strip())
        except (TypeError, ValueError):
            usb_index = 0
        bus = CandlelightBus(channel=usb_index, bitrate=bitrate)
        network = canopen.Network(bus=bus)
        network.connect()
        return network

    network = canopen.Network()
    if system == "Windows":
        network.connect(bustype='pcan',
                        channel=pcan_channel(can_device),
                        bitrate=bitrate)
    elif system == "Linux":
        network.connect(bustype='socketcan', channel=can_device, bitrate=bitrate)
    elif system == "Darwin":
        network.connect(bustype='pcan', channel='PCAN_USBBUS1', bitrate=bitrate)
    else:
        raise RuntimeError(f"unsupported platform for CAN access: {system}")
    return network
