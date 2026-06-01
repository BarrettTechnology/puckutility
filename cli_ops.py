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
from paths import resource_path, FIRMWARE_DIR, CONFIG_DIR, _resolve_path


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


def find_dfu_device():
    """Return True if an STM32 DFU bootloader (0483:df11) is connected."""
    try:
        import usb.core
        return usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID) is not None
    except Exception:
        return False


def _find_can_iface():
    """Return the SocketCAN interface name backed by the CandleLight device, or None."""
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
                                    it. User must unplug and replug the USB cable;
                                    the hardware Boot0 line will be high, so the
                                    STM32 will enter DFU ROM on the next power-up.
           None — device not found or transfer failed.

    Reference: https://netcult.ch/elmue/CANable%20Firmware%20Update/ #Candle_DFU
    """
    try:
        import usb.core
        dev = usb.core.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
        if dev is None:
            return None

        # Detach gs_usb from interface 0 so libusb can send control transfers.
        # The DFU Run-Time interface (1) has no kernel driver.
        # Requires the 90-canable.rules udev rule (MODE="0660", GROUP="plugdev").
        cfg = dev.get_active_configuration()
        for intf in cfg:
            n = intf.bInterfaceNumber
            try:
                if dev.is_kernel_driver_active(n):
                    dev.detach_kernel_driver(n)
            except Exception as e:
                err = str(e).lower()
                if 'access' in err or '13' in err:
                    print("  Permission denied detaching gs_usb driver.")
                    print("  Install the udev rule, replug the adapter, and retry:")
                    print("    sudo cp scripts/90-canable.rules /etc/udev/rules.d/")
                    print("    sudo udevadm control --reload-rules && sudo udevadm trigger")
                    return None

        # Find the DFU Run-Time interface (class=0xFE, subclass=0x01, protocol=0x01).
        dfu_iface = None
        for intf in cfg:
            if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 0x01:
                dfu_iface = intf.bInterfaceNumber
                break
        if dfu_iface is None:
            dfu_iface = 1  # Elmue firmware always puts DFU on interface 1

        # DFU_DETACH: bmRequestType=0x21, bRequest=0x00, wValue=timeout_ms
        dev.ctrl_transfer(0x21, 0x00, 1000, dfu_iface, None)

        # DFU_GETSTATUS immediately after — firmware must still be alive to answer.
        # Response: [bStatus(1), bwPollTimeout(3), bState(1), iString(1)]
        # bState values: 0=appIDLE, 1=appDETACH (DFU spec 1.1 table 5-1)
        try:
            status = dev.ctrl_transfer(0xA1, 0x03, 0, dfu_iface, 6, timeout=500)
            bState = status[4] if len(status) >= 5 else 0
            return 'detach' if bState == 1 else 'idle'
        except Exception:
            # Device may have already started detaching before we read status.
            # Treat as 'idle' — just poll for the DFU device.
            return 'idle'

    except Exception as e:
        print(f"  DFU_DETACH failed: {e}")
        return None


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

    dev = usb.core.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
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


_BUNDLED_FW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'canable-candlelight-multiboard.bin')


def flash_canable(firmware_path=None):
    """Flash CandleLight Multiboard firmware to an STM32G431-based canable via USB DFU.

    Flow:
      1. Send DFU_DETACH to the running Elmue firmware (1d50:606f) to enter DFU ROM.
      2. Flash firmware.bin via dfu-util with ':leave' to trigger post-flash DFU exit.
      3. After flash, send ELM_ReqSetPinStatus(PINOP_Disable, PINID_BOOT0) to the
         Elmue firmware — this writes OPT_BOOT0_Disable (nSWBOOT0=1, nBOOT0=1) so
         the device always boots from Flash and ignores the hardware Boot0 pin.

    No sudo required.  Requires 90-canable.rules udev rules to be installed.
    The firmware path defaults to the bundled canable-candlelight-multiboard.bin.

    Returns True on success.
    """
    import shutil, subprocess, threading, time

    if firmware_path is None:
        firmware_path = _BUNDLED_FW

    if not os.path.isfile(firmware_path):
        print(f"Error: firmware file not found: {firmware_path}")
        return False

    if not shutil.which('dfu-util'):
        print("Error: dfu-util is not installed.")
        print("  Install with:  sudo apt-get install dfu-util")
        return False

    try:
        import usb.core
    except ImportError:
        print("Error: pyusb is not installed — run:  pip install pyusb")
        return False

    # ---- enter DFU mode if not already there --------------------------------
    dfu_dev = usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID)
    if dfu_dev is None:
        can_dev = usb.core.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID)
        if can_dev is None:
            print("Error: no CandleLight (1d50:606f) or DFU (0483:df11) device found.")
            return False

        print("Sending DFU_DETACH to CandleLight…")
        dfu_state = None
        for _attempt in range(3):
            if _attempt:
                time.sleep(1.0)
                print(f"  Retry {_attempt}/2…")
            dfu_state = _enter_dfu_mode()
            if dfu_state is not None:
                break

        if dfu_state is None:
            print("Error: failed to enter DFU mode after 3 attempts.")
            return False

        if dfu_state == 'detach':
            # Elmue firmware re-enabled Boot0 (it was disabled).  The hardware
            # Boot0 line is high on these boards, so a power cycle will put the
            # STM32 into DFU ROM.  No button press needed — just replug USB.
            print()
            print("Boot0 was disabled — the firmware has re-enabled it.")
            print("Please unplug and replug the USB cable to enter DFU mode.")
            print("Waiting for DFU bootloader", end='', flush=True)
            timeout = 60  # 60 s to give the user time to replug
        else:
            # 'idle': firmware will enter DFU ROM on its own after ~300 ms.
            print("Waiting for DFU bootloader", end='', flush=True)
            timeout = 20  # 2 s is plenty; be generous

        for _ in range(timeout * 10):
            time.sleep(0.1)
            if usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID):
                print("  found.")
                break
            print('.', end='', flush=True)
        else:
            print()
            print("DFU bootloader did not enumerate.")
            if dfu_state == 'idle':
                print("If the adapter came back as CandleLight, the current firmware")
                print("may be the legacy (non-Elmue) version which does not support")
                print("software DFU entry.  Flash the Elmue firmware once with the")
                print("Boot0 jumper held, then all future updates work without it.")
            return False
    else:
        print("DFU bootloader already present (0483:df11).")

    time.sleep(0.3)

    # ---- flash firmware (device auto-resets after manifest) -----------------
    print(f"\nFlashing {os.path.basename(firmware_path)}…")
    print("─" * 60)

    flash_ok = False
    proc = subprocess.Popen(
        ['dfu-util', '-d', f'{DFU_VID:04x}:{DFU_PID:04x}',
         '-a', '0', '-s', '0x08000000:leave', '-D', firmware_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        print(line, end='', flush=True)
        if 'File downloaded successfully' in line or 'Download done' in line:
            flash_ok = True
    proc.wait()
    print("─" * 60)

    if not flash_ok:
        print("Flash FAILED.")
        return False

    print("Firmware flashed successfully!")

    # ---- wait for device after flash ----------------------------------------
    # With ':leave' dfu-util triggers DFU_DETACH after manifestation.
    # The STM32G4 ROM may do a software jump to the new Elmue firmware (1d50:606f)
    # or a hardware reset back into DFU ROM (0483:df11) depending on ROM version.
    # Poll for whichever appears first; CandleLight is preferred.
    print("\nWaiting for device after flash", end='', flush=True)
    post_state = None
    for _ in range(100):
        time.sleep(0.1)
        if usb.core.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID):
            post_state = 'candle'
            break
        if usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID):
            post_state = 'dfu'
            break
        print('.', end='', flush=True)

    if post_state is None:
        print("\nWarning: device did not re-enumerate after flash.")
        print("  Run --flash-canable again to retry the Boot0 disable step.")
        return True

    label = 'CandleLight' if post_state == 'candle' else 'DFU mode'
    print(f"  {label}.")

    # ---- disable Boot0 pin --------------------------------------------------
    # OPT_BOOT0_Disable (nSWBOOT0=1, nBOOT0=1) makes the device always boot from
    # Flash, ignoring the hardware Boot0 pin (HIGH on the Multiboard).
    # Primary path: send Elmue vendor command to the running Elmue firmware.
    # Fallback: if still in DFU ROM, re-enter firmware first via AppIdle jump.
    running = [True]

    def _spin():
        chars = '|/-\\'
        i = 0
        while running[0]:
            print(f'\rDisabling Boot0 pin… {chars[i % 4]} ', end='', flush=True)
            i += 1
            time.sleep(0.12)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()

    ok = False
    if post_state == 'candle':
        # Firmware already running — use the Elmue vendor command directly.
        ok = _disable_boot0_via_firmware()
    else:
        # Still in DFU ROM.  The Elmue firmware already called OPT_BOOT0_Enable
        # before jumping to ROM (setting nSWBOOT0=0), so a DFU_DETACH sent to the
        # DFU ROM in dfuIDLE state will cause it to leave DFU and jump to the new
        # Elmue firmware — no hardware reset needed.
        running[0] = False
        t.join(timeout=0.5)
        print('\rWaiting for Elmue firmware to boot', end='', flush=True)

        try:
            import usb.core as _usb
            _dfu = _usb.find(idVendor=DFU_VID, idProduct=DFU_PID)
            if _dfu is not None:
                _dfu.ctrl_transfer(0x21, 0x00, 1000, 0, None, timeout=500)
        except Exception:
            pass

        # Poll for CandleLight after the DFU LEAVE
        for _ in range(80):
            time.sleep(0.1)
            if usb.core.find(idVendor=CANDLELIGHT_VID, idProduct=CANDLELIGHT_PID):
                print("  ready.")
                break
            print('.', end='', flush=True)
        else:
            print()
            print("Elmue firmware did not enumerate after DFU LEAVE.")
            print("  Unplug and replug the adapter, then run --flash-canable again.")
            return True

        running = [True]
        t = threading.Thread(target=_spin, daemon=True)
        t.start()
        ok = _disable_boot0_via_firmware()

    running[0] = False
    t.join(timeout=0.5)

    if ok:
        print('\rDisabling Boot0 pin… done!        ')
    else:
        print('\rDisabling Boot0 pin… failed.')
        print("  The adapter will still work but may enter DFU ROM on power-cycle.")
        print("  Run --flash-canable again, or disable Boot0 with STM32CubeProgrammer.")

    # ---- bring up the SocketCAN interface -----------------------------------
    # _disable_boot0_via_firmware() briefly detaches gs_usb, which destroys can0.
    # On re-attach gs_usb rebinds, creating a new net device ADD event that re-fires
    # the udev RUN+= rule and brings can0 back up.  Poll up to ~8 s for this.
    print("\nWaiting for SocketCAN interface", end='', flush=True)
    iface = None
    for _ in range(80):
        time.sleep(0.1)
        iface = _find_can_iface()
        if iface:
            break
        print('.', end='', flush=True)

    if not iface:
        print()
        print("SocketCAN interface not found.")
        print("The udev rule should have brought it up automatically.")
        print("If it is missing, run:  sudo ./scripts/setup-socketcan.sh")
        return ok

    print(f"  {iface}.")

    # udev should have brought can0 up via the RUN+= rule on the net device ADD.
    # Bring it up ourselves as a fallback in case udev hasn't fired yet.
    try:
        state = open(f'/sys/class/net/{iface}/operstate').read().strip()
    except Exception:
        state = 'unknown'

    if state not in ('up', 'unknown'):
        subprocess.run(
            ['/sbin/ip', 'link', 'set', iface,
             'type', 'can', 'bitrate', '1000000', 'txqueuelen', '1000', 'fd', 'on'],
            capture_output=True,
        )
        subprocess.run(['/sbin/ip', 'link', 'set', iface, 'up'], capture_output=True)
        try:
            state = open(f'/sys/class/net/{iface}/operstate').read().strip()
        except Exception:
            state = 'unknown'

    if state in ('up', 'unknown'):
        print(f"CandleLight is ready on {iface}.")
    else:
        print(f"{iface} found but not up (state={state}).")
        print("Unplug and replug the adapter to trigger the udev auto-bringup rule.")
    return ok
