# Headless CLI helpers for puckutilityapp. No wx dependency — these run when
# the app is invoked with --scan / --flash / --config / --calibrate /
# --system-config (see puckutilityapp.py's __main__).

import os
import time
import math
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

# Conventional locations for system-config payloads, resolved relative to
# this file so they stay correct regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(_HERE, 'firmware')
CONFIG_DIR = os.path.join(_HERE, 'config')


def _resolve_path(value, folder):
    """Resolve a path value read from a system-config INI.

    Strips optional surrounding double-quotes, then:
      - bare filenames (no path separator) are resolved under `folder`
      - absolute paths and any value containing a path separator are
        returned as-is, so older .ini files with full paths still work.
    """
    if not value:
        return value
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    if os.path.isabs(value) or '/' in value or '\\' in value:
        return value
    return os.path.join(folder, value)


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


def _cli_test_encoder(node):
    print("  Testing encoder stability...")
    node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    time.sleep(1)
    t_end = time.time() + 1
    readings = []
    while time.time() < t_end:
        readings.append(node.sdo['PositionFeedback'].raw)
    variation = max(readings) - min(readings)
    max_allowed = 8
    print(f"  Encoder variation: {variation} counts (max {max_allowed})")
    if variation > max_allowed:
        print(f"  WARNING: Encoder readings unstable! "
              f"variation={variation}, max acceptable={max_allowed}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True


def _cli_calibrate_ibias(node):
    print("  Calibrating current sense bias (ibias)...")
    node.sdo["ControlWord"].raw = CLEAR_FAULT
    node.sdo["ControlWord"].raw = SHUTDOWN
    node.sdo["ControlWord"].raw = OP_ENABLED
    node.sdo['Theta_e'].raw = 0x7FFF
    node.sdo['Motor']['ud'].raw = 0
    node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    time.sleep(1)
    for ch in ['Alpha', 'Beta']:
        print(f"  Previous {ch} bias = {node.sdo[ch]['Bias'].raw}")
        filt = node.sdo[ch]['Filtered'].raw
        filt = (filt >> 4) + ((filt & 0x0008) >> 3)
        node.sdo[ch]['Bias'].raw = filt
        print(f"  New {ch} bias = {filt}")
    node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03)
    node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03)
    a_bias = node.sdo['Alpha']['Bias'].raw
    b_bias = node.sdo['Beta']['Bias'].raw
    node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    error = 0.5
    lo, hi = round(2048 * (1 - error)), round(2048 * (1 + error))
    if a_bias > hi or a_bias < lo or b_bias > hi or b_bias < lo:
        print(f"  WARNING: iSense bias out of bounds! "
              f"Alpha={a_bias}, Beta={b_bias}, acceptable range {lo}-{hi}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True


def _cli_calibrate_igainfactor(node):
    print("  Calibrating current sense gain factor (igainfactor)...")
    node.sdo['Alpha']['Gainfactor'].raw = 4096
    node.sdo['Beta']['Gainfactor'].raw = 4096
    node.sdo["ControlWord"].raw = CLEAR_FAULT
    node.sdo["ControlWord"].raw = SHUTDOWN
    node.sdo["ControlWord"].raw = OP_ENABLED
    node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    node.sdo['Theta_e'].raw = 0x7FFF
    cal_current = node.sdo['Calibration']['i_cal'].raw
    i_peak = node.sdo['Calibration']['i_peak'].raw
    if cal_current > i_peak:
        cal_current = i_peak
    time.sleep(1)
    motor_ud = 0
    motor_id = node.sdo['Motor']['id'].raw
    while (motor_id < 1000 and node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current and motor_ud < 32000:
        motor_ud += 100
        node.sdo['Motor']['ud'].raw = motor_ud
        time.sleep(0.05)
    time.sleep(1)
    a_filt = node.sdo['Alpha']['Filtered'].raw
    a_filt = (a_filt >> 4) + ((a_filt & 0x0008) >> 3)
    node.sdo['Theta_e'].raw = -0x4000
    time.sleep(1)
    b_filt = node.sdo['Beta']['Filtered'].raw
    b_filt = (b_filt >> 4) + ((b_filt & 0x0008) >> 3)
    node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    abias = node.sdo['Alpha']['Bias'].raw
    bbias = node.sdo['Beta']['Bias'].raw
    gf_raw = 4096 * (a_filt - abias) / (b_filt - bbias)
    node.sdo['Beta']['Gainfactor'].raw = gf_raw
    gainfactor = round(gf_raw)
    print(f"  New Beta Gainfactor = {gainfactor}")
    node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x06)
    node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x06)
    error = 0.10
    lo, hi = round(4096 * (1 - error)), round(4096 * (1 + error))
    if gainfactor > hi or gainfactor < lo:
        print(f"  WARNING: Beta Gainfactor out of bounds! "
              f"{gainfactor}, acceptable range {lo}-{hi}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True


def _cli_calibrate_enczero(node):
    print("  Calibrating encoder zero...")
    node.sdo["ControlWord"].raw = CLEAR_FAULT
    node.sdo["ControlWord"].raw = SHUTDOWN
    node.sdo["ControlWord"].raw = OP_ENABLED
    node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    node.sdo['Theta_e'].raw = -0x1000
    cal_current = node.sdo['Calibration']['i_cal'].raw
    i_peak = node.sdo['Calibration']['i_peak'].raw
    if cal_current > i_peak:
        cal_current = i_peak
    motor_ud = 0
    motor_id = node.sdo['Motor']['id'].raw
    while (motor_id < 1000 and node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current and motor_ud < 32000:
        motor_ud += 100
        node.sdo['Motor']['ud'].raw = motor_ud
        time.sleep(0.05)
    pos0 = node.sdo['Encoder']['RawPosition'].raw
    startPos1 = node.sdo['PositionFeedback'].raw
    for i in range(int(-0x1000), 0, int(0x1000 / 32)):
        node.sdo['Theta_e'].raw = i
        time.sleep(0.05)
    time.sleep(0.25)
    pos1 = node.sdo['Encoder']['RawPosition'].raw
    zeroPos1 = node.sdo['PositionFeedback'].raw
    node.sdo['Theta_e'].raw = 0x1000
    time.sleep(1)
    startPos2 = node.sdo['PositionFeedback'].raw
    for i in range(int(0x1000), 0, int(-0x1000 / 32)):
        node.sdo['Theta_e'].raw = i
        time.sleep(0.05)
    time.sleep(0.25)
    pos2 = node.sdo['Encoder']['RawPosition'].raw
    zeroPos2 = node.sdo['PositionFeedback'].raw
    enc_res = node.sdo['EncoderConfig']['Resolution'].raw
    poles = node.sdo['Calibration']['poles'].raw
    cts_per_elec = enc_res * 2 / poles
    if abs(pos1 - pos2) > enc_res / 2:
        if pos1 > pos2:
            pos1 += enc_res
        else:
            pos2 += enc_res
    pos = int(((pos1 + pos2) / 2) % cts_per_elec)
    if abs(pos1 - pos0) < cts_per_elec / 2:
        e_polarity = math.copysign(1, pos1 - pos0)
    else:
        e_polarity = -math.copysign(1, pos1 - pos0)
    node.sdo['Calibration']['e_polarity'].raw = e_polarity
    node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x02)
    print(f"  Electrical polarity = {e_polarity}")
    print(f"  Previous e_zero = {node.sdo['Calibration']['e_zero'].raw}, new e_zero = {pos}")
    node.sdo['Calibration']['e_zero'].raw = pos
    node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x01)
    pos_change1 = round(abs(startPos1 - zeroPos1) * (360 / 4096) * poles)
    pos_change2 = round(abs(startPos2 - zeroPos2) * (360 / 4096) * poles)
    node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    error = 0.25
    min_jump = round(22.5 * (1 - error))
    if pos_change1 < min_jump or pos_change2 < min_jump:
        cal_torque = cal_current * node.sdo['Calibration']['kt'].raw / 1000
        print(f"  WARNING: Encoder zero failed! "
              f"Jump1={pos_change1}°, Jump2={pos_change2}°, expected >={min_jump}°, "
              f"cal torque={cal_torque}mNm")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True


def _cli_calibrate_all(node):
    print("  Running full calibration sequence...")
    if not _cli_test_encoder(node):
        print("  Calibration aborted.")
        return False
    if not _cli_calibrate_ibias(node):
        print("  Calibration aborted.")
        return False
    if not _cli_calibrate_igainfactor(node):
        print("  Calibration aborted.")
        return False
    if _cli_calibrate_enczero(node) is False:
        print("  Calibration aborted.")
        return False
    print("  Calibration complete!")
    return True


def _cli_system_config(can_device, ini_path):
    cfg = configparser.ConfigParser()
    cfg.read(ini_path)

    network, found_ids = _cli_connect(can_device)
    network.disconnect()

    configured_ids = []
    for section in cfg.sections():
        node_id = int(cfg[section]['ID'])
        csv_path = _resolve_path(cfg[section]['CSV'], CONFIG_DIR)
        fw_version = cfg[section].get('fw_version')
        fw_path = _resolve_path(cfg[section].get('fw'), FIRMWARE_DIR)
        if node_id not in found_ids:
            print(f"Node {node_id} ({section}) not found on bus, skipping.")
            continue
        print(f"\n--- Configuring node {node_id} ({section}) ---")
        if fw_version and fw_path:
            ver_net = _cli_make_network(can_device)
            ver_node = ver_net.add_node(node_id, 'puck4.eds')
            version = get_version(ver_node.sdo['MfgSoftwareVersion'].raw)
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
                cal_net = _cli_make_network(can_device)
                cal_node = cal_net.add_node(node_id, 'puck4.eds')
                _cli_calibrate_all(cal_node)
                cal_net.disconnect()
