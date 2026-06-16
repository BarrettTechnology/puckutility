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
    index (e.g. '0' -> PCAN_USBBUS1), a SocketCAN-style 'canN' name
    (e.g. 'can0' -> PCAN_USBBUS1, as produced by the GUI port dropdown), or
    a full 'PCAN_USBBUSn' string passed verbatim."""
    text = str(can_device).strip()
    if text.upper().startswith('PCAN_'):
        return text.upper()
    if text.lower().startswith('can') and text[3:].isdigit():
        return 'PCAN_USBBUS' + str(int(text[3:]) + 1)
    try:
        return 'PCAN_USBBUS' + str(int(text) + 1)
    except ValueError:
        raise ValueError(
            f"invalid PCAN device {can_device!r}: expected a bus index "
            f"(e.g. 0 for PCAN_USBBUS1), a 'canN' name, or a PCAN_USBBUSn name")


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


# CANopen 0x1018:2 (Product Code) -> Puck model name.
#
# Two encodings exist in the field:
#   * Legacy firmware reports a small numeric code (e.g. 5707 -> 'P4-16').
#   * Newer firmware packs the 4-character model tag into the UNSIGNED32 as
#     ASCII, most-significant byte first: 0x50343332 == b'P432' -> 'P4-32'.
# `_ProductCodeModels` resolves both: numeric codes are looked up in the table,
# anything else is decoded as a 4-byte ASCII tag on the fly, so a new variant
# only needs an entry in _ASCII_MODEL_TAGS (or none, if the table below already
# covers it).
_ASCII_MODEL_TAGS = {
    b'P416': 'P4-16',
    b'P432': 'P4-32',
    b'P437': 'P4-37',
    b'P442': 'P4-42',
}


def _model_from_ascii(code):
    """Return the model name if `code` is an ASCII-packed model tag (e.g. the
    int 0x50343332 / 1345598258 -> 'P4-32'), else None."""
    try:
        raw = int(code).to_bytes(4, 'big')
    except (TypeError, ValueError, OverflowError):
        return None
    return _ASCII_MODEL_TAGS.get(raw)


class _ProductCodeModels(dict):
    """Maps a product code to a Puck model name, accepting both legacy numeric
    codes (stored as ordinary dict items) and newer ASCII-packed tags (decoded
    on the fly). Drop-in for the plain dict the call sites used before: `.get`
    and `in` both transparently handle the ASCII form."""

    def get(self, code, default=None):
        if dict.__contains__(self, code):
            return dict.__getitem__(self, code)
        model = _model_from_ascii(code)
        return model if model is not None else default

    def __contains__(self, code):
        return dict.__contains__(self, code) or _model_from_ascii(code) is not None


PRODUCT_CODE_MODELS = _ProductCodeModels({
    5707: 'P4-16',
    1323: 'P4-37',
    1950: 'P4-37',
    5755: 'P4-42',
    5760: 'P4-32',
})


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
