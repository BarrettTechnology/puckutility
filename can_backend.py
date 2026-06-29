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
import os
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


def iface_is_candlelight(can_device):
    """True if the SocketCAN interface `can_device` is backed by a CandleLight /
    CANable adapter (the gs_usb kernel driver).

    On Linux this is read straight from sysfs, so it is exact even when a Peak
    PCAN is also plugged in -- only the gs_usb-backed interface reports True. On
    non-Linux platforms there is no SocketCAN interface to inspect, so fall back
    to the USB presence probe.

    Used to gate the USB-level adapter reset: only a CANable exhibits the
    full-TX-buffer pathology a reset fixes, and only a CANable should be
    USB-reset (a Peak recovers on the next reconnect)."""
    if platform.system() != 'Linux':
        return _candlelight_present()
    try:
        driver = os.path.basename(os.path.realpath(
            '/sys/class/net/{}/device/driver'.format(can_device)))
        return driver == 'gs_usb'
    except Exception:
        return False


def list_can_interfaces():
    """Return the CAN interfaces currently present on the system, best-effort.

    Linux: every /sys/class/net entry whose ARPHRD type is CAN (280) -- i.e. real
    SocketCAN interfaces (canX / slcanX), regardless of up/down state. This is what
    the puck apps connect to (the shared udev/systemd setup brings CANable/PCAN
    adapters up as canX automatically).

    Other platforms: returns [] -- there is no SocketCAN to enumerate, so callers
    fall back to the configured device / PCAN channel selection.
    """
    if platform.system() != 'Linux':
        return []
    names = []
    net = '/sys/class/net'
    try:
        for name in sorted(os.listdir(net)):
            try:
                with open(os.path.join(net, name, 'type')) as f:
                    if f.read().strip() == '280':   # ARPHRD_CAN
                        names.append(name)
            except OSError:
                continue
    except OSError:
        pass
    return names


def is_tx_buffer_error(exc):
    """True if `exc` is the 'transmit buffer full' CAN error.

    A device on the bus that never ACKs (classically a blank/erased puck) leaves
    frames unacknowledged until SocketCAN's TX buffer fills and python-can raises
    CanOperationError('Transmit buffer full'). This is the only CAN error a
    USB-level adapter reset actually clears -- other CanErrors (bus-off, network
    down, device unplugged) recover on a plain reconnect/rescan, so they must NOT
    trigger a reset."""
    return 'buffer' in str(exc).lower()


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


def model_from_product_code(code):
    """Resolve a CANopen product code (0x1018:2) to a Puck model name, or None.

    Single converged entry point shared by puckutility, pucktuner, and
    P4-checkout. Accepts BOTH encodings: legacy small-integer codes (e.g.
    5760 -> 'P4-32') and newer ASCII-packed codes (e.g. 0x50343332 = b'P432'
    -> 'P4-32'). Thin wrapper over PRODUCT_CODE_MODELS so callers can use a
    plain function instead of the dict-with-.get() form."""
    return PRODUCT_CODE_MODELS.get(code)


def make_network(can_device, bitrate=1_000_000, fd=False, data_bitrate=None):
    """Create and connect a canopen.Network. The adapter is auto-selected:
    CandleLight if present on USB, otherwise PCAN (Windows/macOS) or
    SocketCAN (Linux).

    `can_device` is the channel selector for the PCAN/SocketCAN paths. For
    CandleLight, integer values select which USB instance to open when
    multiple adapters are connected (0 = first found).

    `fd` enables CAN-FD frame transmission; `data_bitrate` is the FD data-phase
    rate (e.g. 5_000_000). For SocketCAN the data bitrate is set on the LINK
    (`ip link ... dbitrate`), so only `fd` is passed to python-can there; the
    CandleLight/PCAN drivers take the data_bitrate directly. Both default off so
    callers that don't opt in stay on classic CAN.
    """
    system = platform.system()

    if system == "Windows" and _candlelight_present():
        try:
            usb_index = int(str(can_device).strip())
        except (TypeError, ValueError):
            usb_index = 0
        bus = CandlelightBus(channel=usb_index, bitrate=bitrate,
                             fd=fd, data_bitrate=data_bitrate)
        network = canopen.Network(bus=bus)
        network.connect()
        return network

    # PCAN/CandleLight configure the FD data phase in software; SocketCAN reads
    # it from the link, so it only wants fd=True (passing data_bitrate would be
    # an unexpected kwarg). Build the FD kwargs per backend accordingly.
    pcan_fd_kwargs = {'fd': True, 'data_bitrate': data_bitrate} if fd else {}

    network = canopen.Network()
    if system == "Windows":
        network.connect(bustype='pcan',
                        channel=pcan_channel(can_device),
                        bitrate=bitrate, **pcan_fd_kwargs)
    elif system == "Linux":
        network.connect(bustype='socketcan', channel=can_device,
                        bitrate=bitrate, **({'fd': True} if fd else {}))
    elif system == "Darwin":
        network.connect(bustype='pcan', channel='PCAN_USBBUS1',
                        bitrate=bitrate, **pcan_fd_kwargs)
    else:
        raise RuntimeError(f"unsupported platform for CAN access: {system}")

    if fd:
        _enable_fd_frames(network)
    return network


def _enable_fd_frames(network):
    """Make outgoing canopen frames actual CAN-FD frames.

    python-can transmits a Message as a CLASSIC frame unless ``msg.is_fd`` is
    set -- and canopen builds plain (classic) Messages, so on an FD-enabled
    SocketCAN/PCAN socket the frames still go out 8-byte at the 1 Mbit nominal
    rate. Wrap the bus ``send`` to flag every outgoing message FD + BRS so the
    data phase actually uses the link's FD data bitrate. (The CandleLight driver
    does this inside its own send, so it is not wrapped here.)
    """
    bus = network.bus
    _orig_send = bus.send

    def _send_fd(msg, *args, **kwargs):
        msg.is_fd = True
        msg.bitrate_switch = True
        return _orig_send(msg, *args, **kwargs)

    bus.send = _send_fd
