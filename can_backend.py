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


def _candlelight_present():
    """Return True if a CandleLight CANable is enumerated on USB. Returns
    False (rather than raising) for any error so callers can fall back to
    PCAN whenever detection is inconclusive."""
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
        from sandbox.candlelight_bus import CandlelightBus
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
