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
