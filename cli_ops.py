# Headless CLI helpers for puckutilityapp. Runs when the app is invoked with
# --scan / --flash / --config / --calibrate / --system-config.
#
# Calibration logic lives entirely in calibrate_menu.py. The _HeadlessCalibrateAdapter
# class below satisfies the wx-frame interface that the calibrate mixin expects, so
# the CLI and GUI always execute identical code paths.

import os
import sys
import time
import platform
import configparser

import canopen
import canopen_runner
import flashp4
from flashp4 import get_version
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE,
)
from paths import resource_path, FIRMWARE_DIR, CONFIG_DIR, _resolve_path, MAIN_DIR


# ---------------------------------------------------------------------------
# wx bootstrap — wx is always in the venv (it's listed in requirements.txt).
# A minimal App is required so that wx.Yield() inside _sleep_responsive() is
# a safe no-op rather than raising "No wxApp" on some platforms.
# ---------------------------------------------------------------------------
import wx as _wx
if not _wx.GetApp():
    _wx.App(False)


# ---------------------------------------------------------------------------
# Headless adapter — lets calibrate_menu methods run without a live wx frame.
# ---------------------------------------------------------------------------
from calibrate_menu import calibrate as _CalibrateMixin


class _HeadlessStatusBar:
    def SetStatusText(self, text, number=0):
        if number == 0 and text:
            print(f"  [{text}]")
    def Update(self):  pass
    def Refresh(self): pass


class _HeadlessChoice:
    def GetSelection(self):    return 0
    def SetSelection(self, v): pass


class _HeadlessCalibrateAdapter(_CalibrateMixin):
    """Thin wx-free wrapper around the calibrate mixin for CLI use."""

    def __init__(self, node, network=None):
        self.node             = node
        self.network          = network
        self.ID               = node.id
        self.ADC_ON           = False
        self.adcWasON         = False
        self.lastMode         = 0
        self.requireCal       = True
        self.frame_statusbar  = _HeadlessStatusBar()
        self.choice_test      = _HeadlessChoice()

    # --- interface stubs expected by the calibrate mixin ---
    def getID(self):               return self.node.id
    def check_for_node(self):      return True
    def Disable(self):             pass
    def Enable(self):              pass
    def OnStartTask(self, event):  pass
    def OnTaskComplete(self):      pass
    def UpdateUI(self, value):     pass
    def on_off_adc(self, event):   pass

    # --- prompt overrides: CLI uses stdin instead of wx dialogs ---
    def _prompt(self, title, msg):
        print(f"\n  WARNING [{title}]\n  {msg}")
        return input("  Continue calibration? [y/n]: ").strip().lower() == 'y'

    def _prompt_ok(self, title, msg):
        print(f"\n  ERROR [{title}]\n  {msg}")


# ---------------------------------------------------------------------------
# Public CLI calibration helpers — thin wrappers around the shared methods.
# ---------------------------------------------------------------------------

def _cli_test_encoder(node, network=None):
    return _HeadlessCalibrateAdapter(node, network).test_encoder(None, calAll=True)


def _cli_calibrate_ibias(node, network=None):
    return _HeadlessCalibrateAdapter(node, network).calibrate_ibias(
        None, calAll=True, _upd=lambda v: None)


def _cli_calibrate_igainfactor(node, network=None):
    return _HeadlessCalibrateAdapter(node, network).calibrate_igainfactor(
        None, calAll=True, _upd=lambda v: None)


def _cli_calibrate_enczero(node, network=None):
    return _HeadlessCalibrateAdapter(node, network).calibrate_enczero(
        None, calAll=True, _upd=lambda v: None)


def _cli_calibrate_all(node, network=None):
    adapter = _HeadlessCalibrateAdapter(node, network)
    print(f"  Running full calibration sequence for node {node.id}...")
    if not adapter.test_encoder(None, calAll=True):
        print("  Calibration aborted.")
        return False
    if not adapter.calibrate_ibias(None, calAll=True, _upd=lambda v: None):
        print("  Calibration aborted.")
        return False
    if not adapter.calibrate_igainfactor(None, calAll=True, _upd=lambda v: None):
        print("  Calibration aborted.")
        return False
    if adapter.calibrate_enczero(None, calAll=True, _upd=lambda v: None) is False:
        print("  Calibration aborted.")
        return False
    print("  Calibration complete!")
    return True


# ---------------------------------------------------------------------------
# Network / flash / config helpers
# ---------------------------------------------------------------------------

class CLIProgress:
    """Queue-compatible progress sink for CLI use (replaces multiprocessing.Queue)."""
    def put(self, value):
        if isinstance(value, int):
            print(f"\rProgress: {value}%  ", end="", flush=True)
        else:
            print()  # newline after the progress line


def _cli_make_network(can_device):
    network = canopen.Network()
    system = platform.system()
    if system == "Windows":
        network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
    elif system == "Linux":
        network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)
    elif system == "Darwin":
        network.connect(bustype='pcan', channel='PCAN_USBBUS1', bitrate=1000000)
    return network


def _cli_connect(can_device):
    """Connect to CAN bus and scan for nodes. Returns (network, node_ids)."""
    network = _cli_make_network(can_device)
    network.scanner.reset()
    network.scanner.search()
    time.sleep(0.5)
    nodes = list(network.scanner.nodes)
    print(f"Found {len(nodes)} node(s): {nodes}")
    return network, nodes


def _cli_flash(can_device, node_id, fw_path):
    print(f"Flashing node {node_id} with {fw_path}...")
    result = flashp4.flash(can_device, node_id, fw_path, CLIProgress())
    if result:
        print(f"Flash failed: {flashp4.flash_result.get_string[result]}")
        return False
    print("Flash succeeded.")
    time.sleep(0.5)
    return True


def _cli_config(can_device, node_id, csv_path):
    print(f"Uploading config to node {node_id} from {csv_path}...")
    canopen_runner.start(can_device, node_id, 'puck4.eds', csv_path, CLIProgress())
    # Mirror file_to_p4: save all OD entries to EEPROM then reboot
    save_net = _cli_make_network(can_device)
    save_node = save_net.add_node(node_id, 'puck4.eds')
    print("  Saving to EEPROM...")
    default_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 1.0
    save_node.sdo['Save']['All'].raw = 0x65766173  # 'save'
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout
    print("  Rebooting puck...")
    save_net.send_message(0x0, [0x81, node_id])
    time.sleep(0.5)
    save_net.disconnect()


def _cli_system_config(can_device, ini_path):
    cfg = configparser.ConfigParser()
    cfg.read(ini_path)

    network, found_ids = _cli_connect(can_device)
    network.disconnect()

    configured_ids = []
    for section in cfg.sections():
        node_id  = int(cfg[section]['ID'])
        csv_path = _resolve_path(cfg[section]['CSV'], CONFIG_DIR)
        fw_version = cfg[section].get('fw_version')
        fw_path  = _resolve_path(cfg[section].get('fw'), FIRMWARE_DIR)
        if node_id not in found_ids:
            print(f"Node {node_id} ({section}) not found on bus, skipping.")
            continue
        print(f"\n--- Configuring node {node_id} ({section}) ---")
        if fw_version and fw_path:
            ver_net  = _cli_make_network(can_device)
            ver_node = ver_net.add_node(node_id, 'puck4.eds')
            version  = get_version(ver_node.sdo['MfgSoftwareVersion'].raw)
            ver_net.disconnect()
            if version != fw_version:
                print(f"  Firmware {version} → updating to {fw_version}...")
                time.sleep(0.2)
                if not _cli_flash(can_device, node_id, fw_path):
                    print(f"  Skipping config for node {node_id} due to flash failure.")
                    continue
            else:
                print(f"  Firmware {version} up to date.")
        _cli_config(can_device, node_id, csv_path)
        configured_ids.append(node_id)

    if configured_ids:
        resp = input("\nCalibration required after configuration. Calibrate now? [y/n]: ").strip().lower()
        if resp == 'y':
            for node_id in configured_ids:
                print(f"\n--- Calibrating node {node_id} ---")
                cal_net  = _cli_make_network(can_device)
                cal_node = cal_net.add_node(node_id, 'puck4.eds')
                _cli_calibrate_all(cal_node, cal_net)
                cal_net.disconnect()


# ---------------------------------------------------------------------------
# CandleLight DFU firmware flash
# ---------------------------------------------------------------------------

CANDLELIGHT_VID = 0x1d50
CANDLELIGHT_PID = 0x606f
DFU_VID         = 0x0483
DFU_PID         = 0xdf11


_USB_BACKEND_CACHE = []


def _usb_backend():
    """Resolve the pyusb backend. On Windows, prefer the libusb DLL shipped
    inside the libusb-package wheel so end-users don't have to install libusb
    system-wide. On other platforms, return None (let pyusb auto-discover the
    system libusb)."""
    if _USB_BACKEND_CACHE:
        return _USB_BACKEND_CACHE[0]
    backend = None
    if platform.system() == "Windows":
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            backend = None
    _USB_BACKEND_CACHE.append(backend)
    return backend


def _usb_find(**kwargs):
    """usb.core.find with the platform-appropriate backend wired in."""
    import usb.core
    return usb.core.find(backend=_usb_backend(), **kwargs)


def find_dfu_device():
    """Return True if an STM32 DFU bootloader (0483:df11) is connected."""
    try:
        import usb.core
        return _usb_find(idVendor=DFU_VID, idProduct=DFU_PID) is not None
    except Exception:
        return False


def _find_can_iface():
    """Return the SocketCAN interface name backed by the CandleLight device, or None."""
    if platform.system() != "Linux":
        return None
    import glob
    try:
        for iface_path in glob.glob('/sys/class/net/can*'):
            path = os.path.realpath(iface_path)
            for _ in range(12):
                path = os.path.dirname(path)
                vid_f = os.path.join(path, 'idVendor')
                if os.path.exists(vid_f):
                    try:
                        vid = int(open(vid_f).read().strip(), 16)
                        pid = int(open(os.path.join(path, 'idProduct')).read().strip(), 16)
                        if vid == CANDLELIGHT_VID and pid == CANDLELIGHT_PID:
                            return os.path.basename(iface_path)
                    except Exception:
                        pass
                    break
    except Exception:
        pass
    return None


def _enter_dfu_mode():
    """Send DFU_DETACH to a live CandleLight device (1d50:606f).

    Implements the Elmue CANable 2.5 firmware DFU entry protocol:
      1. Send DFU_DETACH to the DFU Run-Time interface (interface 1).
      2. Immediately read DFU_GetStatus (6 bytes) to check the result.
      3. Return the bState byte:
           'idle'   (appIDLE=0)   — firmware will enter DFU ROM in ~300 ms.
           'detach' (appDETACH=1) — Boot0 was disabled; firmware has re-enabled
                                    it (pending). The STM32 needs a power-cycle
                                    for the new option bytes to take effect so it
                                    enters DFU ROM on the next power-up.
           None — device not found or transfer failed.

    DFU_DETACH targets interface 1 (class 0xFE) which has no Linux kernel driver,
    so gs_usb on interface 0 does NOT need to be detached.  This keeps can0 alive.

    Reference: https://netcult.ch/elmue/CANable%20Firmware%20Update/ #Candle_DFU
    """
    try:
        import usb.core
        dev = _usb_find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
        if dev is None:
            return None

        # Find the DFU Run-Time interface (class=0xFE, subclass=0x01).
        # It has no Linux kernel driver, so no detach_kernel_driver() is needed.
        try:
            cfg = dev.get_active_configuration()
            dfu_iface = next(
                (intf.bInterfaceNumber for intf in cfg
                 if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 0x01),
                1,  # Elmue firmware always puts DFU on interface 1
            )
        except Exception:
            dfu_iface = 1

        # DFU_DETACH: bmRequestType=0x21, bRequest=0x00, wValue=timeout_ms, wIndex=iface
        try:
            dev.ctrl_transfer(0x21, 0x00, 1000, dfu_iface, None)
        except Exception as e:
            err = str(e).lower()
            if 'access' in err or 'errno 13' in err or '[errno 13]' in err:
                print("  Permission denied accessing USB device.")
                if platform.system() == "Windows":
                    print("  Run Zadig (https://zadig.akeo.ie/) and bind the WinUSB driver to:")
                    print("    CandleLight  (USB ID 1d50:606f)")
                    print("    STM32  BOOTLOADER  (USB ID 0483:df11)")
                    print("  Then replug the adapter and retry.")
                else:
                    print("  Install the udev rule, replug the adapter, and retry:")
                    print("    sudo cp scripts/90-canable.rules /etc/udev/rules.d/")
                    print("    sudo udevadm control --reload-rules && sudo udevadm trigger")
                return None
            raise

        # DFU_GETSTATUS immediately after — firmware must still be alive to answer.
        # Response: [bStatus(1), bwPollTimeout(3), bState(1), iString(1)]
        # bState values: 0=appIDLE, 1=appDETACH (DFU spec 1.1 table 5-1)
        try:
            status = dev.ctrl_transfer(0xA1, 0x03, 0, dfu_iface, 6, timeout=500)
            bState = status[4] if len(status) >= 5 else 0
            return 'detach' if bState == 1 else 'idle'
        except Exception:
            # Device may have already started detaching before we read status.
            return 'idle'

    except Exception as e:
        print(f"  DFU_DETACH failed: {e}")
        return None


def _try_usb_power_cycle(_dev_unused):
    """Power-cycle the USB port hosting the CandleLight adapter using uhubctl.

    Uses sysfs to find the device's USB location, then tries to power-cycle
    the device's port on its parent hub.  If that hub does not support power
    switching (e.g. a basic consumer hub), walks up to the grandparent hub.

    Returns True if the power cycle was successfully initiated.
    Requires uhubctl to be installed.
    """
    if platform.system() != "Linux":
        return False
    import shutil, subprocess, glob

    if not shutil.which('uhubctl'):
        return False
    try:
        # Locate the device in sysfs (e.g. "1-1.5.2")
        dev_name = None
        for vf in glob.glob('/sys/bus/usb/devices/*/idVendor'):
            try:
                if (open(vf).read().strip() == f'{CANDLELIGHT_VID:04x}' and
                        open(vf.replace('idVendor', 'idProduct')).read().strip()
                        == f'{CANDLELIGHT_PID:04x}'):
                    dev_name = os.path.basename(os.path.dirname(vf))
                    break
            except Exception:
                pass
        if not dev_name or '-' not in dev_name:
            return False

        # "1-1.5.2" → bus="1", path_parts=["1","5","2"]
        bus_str, path_str = dev_name.split('-', 1)
        path_parts = path_str.split('.')

        # Build a list of (hub_location, port) pairs from closest to root.
        # Try each in turn; the first that uhubctl accepts wins.
        attempts = []
        for i in range(len(path_parts) - 1, -1, -1):
            port_val = int(path_parts[i])
            if i == 0:
                hub_loc = bus_str                                           # root hub
            else:
                hub_loc = bus_str + '-' + '.'.join(path_parts[:i])
            attempts.append((hub_loc, port_val))

        for hub_loc, port_val in attempts:
            r = subprocess.run(
                ['uhubctl', '-l', hub_loc, '-p', str(port_val), '-a', 'off'],
                capture_output=True, text=True, timeout=5,
            )
            out = r.stdout + r.stderr
            if r.returncode != 0 or 'no compatible' in out.lower():
                continue  # hub at this level doesn't support power switching

            # Verify VBUS was actually cut — some hubs accept the command but
            # don't physically switch power (e.g. Intel integrated hubs).
            # If the device is still visible after 2 s, the cut didn't happen.
            import usb.core as _usb
            vanished = False
            for _ in range(20):
                time.sleep(0.1)
                if _usb.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID) is None:
                    vanished = True
                    break

            if not vanished:
                # Hub didn't cut power — restore and try the next hub level.
                subprocess.run(
                    ['uhubctl', '-l', hub_loc, '-p', str(port_val), '-a', 'on'],
                    capture_output=True, text=True, timeout=5,
                )
                continue

            # Device is gone — VBUS was cut.  Restore power after a brief hold.
            time.sleep(0.5)
            subprocess.run(
                ['uhubctl', '-l', hub_loc, '-p', str(port_val), '-a', 'on'],
                capture_output=True, text=True, timeout=5,
            )
            return True

        return False
    except Exception:
        return False


def _disable_boot0_via_firmware():
    """Disable Boot0 pin by sending Elmue vendor commands to the running firmware.

    The Elmue firmware (1d50:606f) exposes ELM_ReqSetPinStatus which calls
    system_set_option_bytes(OPT_BOOT0_Disable) internally — writing nSWBOOT0=1,
    nBOOT0=1 so the STM32G431 always boots from Flash and ignores the hardware
    Boot0 pin (which is permanently HIGH on the Multiboard).

    This matches the behaviour of the Elmue HUD ECU Hacker programmer.
    The Elmue protocol must be enabled first (GS_ReqSetDeviceMode with
    ELM_DevFlagProtocolElmue) before ELM commands are accepted.

    Returns True on success.
    """
    import usb.core, struct

    # Constants from Elmue candlelight_def.h
    GS_REQ_SET_DEVICE_MODE   = 2       # GS_ReqSetDeviceMode
    ELM_REQ_SET_PIN_STATUS   = 24      # ELM_ReqSetPinStatus
    ELM_DEV_FLAG_PROTO_ELMUE = 0x4000  # ELM_DevFlagProtocolElmue
    GS_MODE_RESET            = 0       # GS_ModeReset
    PINOP_DISABLE            = 5       # PINOP_Disable
    PINID_BOOT0              = 1       # PINID_BOOT0

    dev = _usb_find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
    if dev is None:
        return False

    detached = []
    try:
        # bmRequestType=0x21 (Class | Interface) with wIndex=0 targets interface 0.
        # Linux rejects userspace control transfers to an interface that has a kernel
        # driver bound (EBUSY), so detach gs_usb briefly.  The finally block
        # re-attaches it; udev fires on the resulting net device ADD and brings
        # can0 back up automatically.
        try:
            cfg = dev.get_active_configuration()
            for intf in cfg:
                n = intf.bInterfaceNumber
                if dev.is_kernel_driver_active(n):
                    dev.detach_kernel_driver(n)
                    detached.append(n)
        except Exception:
            pass

        # Enable Elmue extended commands, then write OPT_BOOT0_Disable.
        mode_data = struct.pack('<II', GS_MODE_RESET, ELM_DEV_FLAG_PROTO_ELMUE)
        dev.ctrl_transfer(0x21, GS_REQ_SET_DEVICE_MODE, 0, 0, mode_data, timeout=2000)

        # kPinStatus: uint16 Operation, uint16 PinID, uint32 Reserved1, uint32 Reserved2
        pin_data = struct.pack('<HHII', PINOP_DISABLE, PINID_BOOT0, 0, 0)
        dev.ctrl_transfer(0x21, ELM_REQ_SET_PIN_STATUS, 0, 0, pin_data, timeout=2000)
        return True
    except Exception as e:
        print(f"  Elmue vendor command failed: {e}")
        return False
    finally:
        for n in detached:
            try:
                dev.attach_kernel_driver(n)
            except Exception:
                pass


_BUNDLED_FW = os.path.join(MAIN_DIR, 'canable-candlelight-multiboard.bin')


def flash_canable(firmware_path=None, verbose=False):
    """Flash CandleLight Multiboard firmware to an STM32G431-based canable via USB DFU.

    No sudo required.  Requires 90-canable.rules udev rules to be installed.
    The firmware path defaults to the bundled canable-candlelight-multiboard.bin.

    Returns True on success.
    """
    import shutil, subprocess, threading, time

    if firmware_path is None:
        firmware_path = _BUNDLED_FW

    # ---- preflight ----------------------------------------------------------
    if not os.path.isfile(firmware_path):
        print(f"Error: firmware file not found: {firmware_path}")
        return False
    if not shutil.which('dfu-util'):
        print("Error: dfu-util is not installed.")
        _sys = platform.system()
        if _sys == "Windows":
            print("  Install with one of:")
            print("    choco install dfu-util")
            print("    scoop install dfu-util")
            print("  or download from https://dfu-util.sourceforge.net/ and add to PATH.")
        elif _sys == "Darwin":
            print("  Install with:  brew install dfu-util")
        else:
            print("  Install with:  sudo apt-get install dfu-util")
        return False
    try:
        import usb.core
    except ImportError:
        print("Error: pyusb is not installed -- run:  pip install pyusb")
        return False

    dfu_dev = _usb_find(idVendor=DFU_VID, idProduct=DFU_PID)
    if dfu_dev is None:
        if _usb_find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID) is None:
            print("Error: no CandleLight (1d50:606f) or DFU (0483:df11) device found.")
            return False

    # ---- spinner / output helpers -------------------------------------------
    _COLS = 60
    phase  = ['entering DFU']
    spun   = [True]
    _chars = '|/-\\'

    def _spin():
        i = 0
        while spun[0]:
            print(f'\rFlashing CandleLight firmware  [{phase[0]}]  {_chars[i % 4]} ',
                  end='', flush=True)
            i += 1
            time.sleep(0.1)

    def _vprint(*args, **kwargs):
        if verbose:
            print(*args, **kwargs)

    def _stop_spinner():
        spun[0] = False
        if _t[0] is not None:
            _t[0].join(timeout=0.5)
            print(f'\r{" " * _COLS}\r', end='', flush=True)

    def _fail(msg):
        _stop_spinner()
        print(msg)

    _t = [None]
    if not verbose:
        _t[0] = threading.Thread(target=_spin, daemon=True)
        _t[0].start()

    # ---- enter DFU mode if not already there --------------------------------
    if dfu_dev is None:
        _candle_for_cycle = _usb_find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
        _vprint("Sending DFU_DETACH to CandleLight...")

        dfu_state = None
        for _attempt in range(3):
            if _attempt:
                time.sleep(1.0)
                _vprint(f"  Retry {_attempt}/2...")
            dfu_state = _enter_dfu_mode()
            if dfu_state is not None:
                break

        if dfu_state is None:
            _fail("Error: failed to enter DFU mode after 3 attempts.")
            return False

        if dfu_state == 'detach':
            cycled = _candle_for_cycle is not None and _try_usb_power_cycle(_candle_for_cycle)
            if cycled:
                _vprint("USB port power-cycled automatically.")
                timeout = 15
            else:
                # User must act — pause spinner and print the prompt.
                if not verbose:
                    _stop_spinner()
                print("Please unplug and replug the USB cable to enter DFU mode.")
                print("Waiting for DFU bootloader", end='', flush=True)
                for _ in range(600):
                    time.sleep(0.1)
                    if _usb_find(idVendor=DFU_VID, idProduct=DFU_PID):
                        print("  found.")
                        break
                    print('.', end='', flush=True)
                else:
                    print()
                    print("Error: DFU bootloader did not enumerate after replug.")
                    return False
                # Skip the normal DFU-wait below; already found.
                dfu_state = 'idle'
                timeout = 0

        else:
            timeout = 20  # 'idle': firmware jumps on its own after ~300 ms

        _vprint("Waiting for DFU bootloader", end='', flush=True)
        for _ in range(timeout * 10):
            time.sleep(0.1)
            if _usb_find(idVendor=DFU_VID, idProduct=DFU_PID):
                _vprint("  found.")
                break
            _vprint('.', end='', flush=True)
        else:
            # DFU ROM never appeared.  If the device came back as CandleLight
            # (AppDetach path, Boot0=LOW), OPT_BOOT0_Enable is now in the shadow
            # register; a second DFU_DETACH takes the software-jump path.
            if dfu_state == 'detach' and _usb_find(
                    idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID):
                _vprint("\nDevice re-enumerated as CandleLight (Boot0 LOW).")
                _vprint("Sending second DFU_DETACH -- firmware now uses software jump path...")
                dfu_state = _enter_dfu_mode()
                if dfu_state == 'idle':
                    _vprint("Waiting for DFU bootloader", end='', flush=True)
                    for _ in range(200):
                        time.sleep(0.1)
                        if _usb_find(idVendor=DFU_VID, idProduct=DFU_PID):
                            _vprint("  found.")
                            break
                        _vprint('.', end='', flush=True)
                    else:
                        _fail("Error: DFU bootloader did not enumerate on second attempt.")
                        return False
                else:
                    _fail(f"Error: unexpected DFU state after second DETACH: {dfu_state}")
                    return False
            else:
                _fail("Error: DFU bootloader did not enumerate.")
                return False
    else:
        _vprint("DFU bootloader already present (0483:df11).")

    time.sleep(0.3)

    # ---- flash firmware -----------------------------------------------------
    phase[0] = 'flashing'
    _vprint(f"\nFlashing {os.path.basename(firmware_path)}...")
    if verbose:
        print("-" * _COLS)

    flash_ok = False
    dfu_errors = []
    proc = subprocess.Popen(
        ['dfu-util', '-d', f'{DFU_VID:04x}:{DFU_PID:04x}',
         '-a', '0', '-s', '0x08000000:leave', '-D', firmware_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        # Suppress benign get_status error: ':leave' causes the device to jump
        # to the application before dfu-util can poll status — expected behavior.
        if 'Error during download get_status' in line:
            continue
        if 'File downloaded successfully' in line or 'Download done' in line:
            flash_ok = True
        elif 'dfu-util: Error' in line or 'dfu-util: Warning' in line:
            dfu_errors.append(line.rstrip())
        _vprint(line, end='', flush=True)
    proc.wait()

    if verbose:
        print("-" * _COLS)

    if not flash_ok:
        _fail("Error: firmware flash failed.")
        for e in dfu_errors:
            print(f"  {e}")
        return False

    _vprint("Firmware flashed successfully!")

    # ---- wait for device after flash ----------------------------------------
    # With ':leave' dfu-util sends DFU_DETACH; the STM32 DFU ROM may briefly
    # re-enumerate as 0483:df11 before jumping to the application.  Always wait
    # for CandleLight (1d50:606f); only fall back to 'dfu' if it never appears.
    phase[0] = 'reconnecting'
    _vprint("\nWaiting for device after flash", end='', flush=True)
    post_state = None
    for _ in range(100):
        time.sleep(0.1)
        if _usb_find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID):
            post_state = 'candle'
            break
        _vprint('.', end='', flush=True)

    if post_state is None:
        if _usb_find(idVendor=DFU_VID, idProduct=DFU_PID):
            post_state = 'dfu'
        else:
            _fail("Warning: device did not re-enumerate after flash.")
            print("  Unplug and replug the adapter, then run --flash-canable again.")
            return True

    _vprint(f"  {'CandleLight' if post_state == 'candle' else 'DFU mode'}.")

    # After the flash the device re-enumerates fresh.  gs_usb binds cleanly and
    # udev brings can0 up.  Boot0=LOW on the Multiboard so OPT_BOOT0_Enable
    # (written by the firmware before jumping to DFU ROM) is functionally
    # equivalent to OPT_BOOT0_Disable — the device always boots from Flash.

    # Windows has no SocketCAN — flashing is complete once the device
    # re-enumerates as CandleLight.  CAN access on Windows goes through PCAN.
    if platform.system() != "Linux":
        if not verbose:
            _stop_spinner()
        if post_state == 'candle':
            print("CandleLight firmware flashed successfully.")
            return True
        print("Warning: device re-enumerated in DFU mode after flash.")
        print("  Unplug and replug the adapter to return to CandleLight mode.")
        return True

    # ---- bring up the SocketCAN interface -----------------------------------
    # The fresh enumeration triggers a udev net ADD event that runs the RUN+=
    # rule (as root) to configure and bring up can0.  Poll until IFF_UP is set.
    phase[0] = 'starting can0'

    def _iface_is_up(name):
        try:
            return bool(int(open(f'/sys/class/net/{name}/flags').read().strip(), 16) & 0x1)
        except Exception:
            return False

    _vprint("\nWaiting for SocketCAN interface", end='', flush=True)
    iface = None
    for _ in range(120):  # 12 s
        time.sleep(0.1)
        candidate = _find_can_iface()
        if candidate and _iface_is_up(candidate):
            iface = candidate
            break
        _vprint('.', end='', flush=True)

    if iface is None:
        # Interface appeared but udev hasn't brought it up yet — try explicitly.
        iface = _find_can_iface()
        if iface:
            subprocess.run(
                ['/sbin/ip', 'link', 'set', iface,
                 'type', 'can', 'bitrate', '1000000', 'txqueuelen', '1000', 'fd', 'on'],
                capture_output=True,
            )
            subprocess.run(['/sbin/ip', 'link', 'set', iface, 'up'],
                           capture_output=True)

    if not verbose:
        _stop_spinner()

    if not iface:
        print("Error: SocketCAN interface not found.")
        print("  Run:  sudo ./scripts/setup-socketcan.sh")
        return False

    if _iface_is_up(iface):
        print(f"CandleLight is ready on {iface}.")
        return True
    else:
        print(f"Warning: {iface} is not up (udev rule may not have run).")
        print(f"  Run:  sudo ip link set {iface} type can bitrate 1000000 txqueuelen 1000 fd on")
        print(f"        sudo ip link set {iface} up")
        return False
