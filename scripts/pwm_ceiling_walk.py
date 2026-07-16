#!/usr/bin/env python3
"""PWM CEILING WALK -- find the highest PWM frequency the control loop runs CLEAN at.

The PWM ceiling is set by the SLOWEST pwm_execute() case: each of the 4 cases must
finish inside one PWM period. When a case (case 4 = PI/decouple/SVM/duty) overruns
the period, the duty update is late/missed -> commutation degrades -> the motor draws
more current for the same speed, jitters, or faults. This walks the PWM rate up and
watches for exactly that, so you can set a safe firmware default (highest clean - margin).

For each rate it: sets 0x3001:1, saves to NV, reboots (re-derives dt+gains, cal
untouched), spins at a moderate velocity, and grades the hold against the baseline
(lowest) rate. Overrun is speed-independent (compute time is fixed), so a moderate,
cool spin reveals it -- no need to chase top speed.

Usage:
  scripts/pwm_ceiling_walk.py [--start 100] [--stop 125] [--step 5]
                              [--rpm 2500] [--dwell 4] [--cool 3]
                              [can_device] [node_id]
Examples:
  scripts/pwm_ceiling_walk.py                       # 100->125 kHz by 5, can0 127
  scripts/pwm_ceiling_walk.py --start 95 --stop 130 --step 5 --rpm 3000

Safety: brief spins (default 4 s) at a moderate speed; drive idled on exit / Ctrl-C.
No winding thermistor on these motors -- --cool gives idle time between steps.
On exit the PWM is left at the highest CLEAN rate found (or --start if none passed).
"""
import argparse
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import can_backend
from canopen_runner import CLEAR_FAULT, SHUTDOWN, OP_ENABLED, MODE_IDLE, MODE_PROFILE_VEL

SAVE_MAGIC = 0x65766173  # 'save' -> 0x1010:1 Save All


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _std(a):
    if len(a) < 2:
        return 0.0
    m = _mean(a)
    return (sum((x - m) ** 2 for x in a) / len(a)) ** 0.5


def set_pwm(node, hz):
    """Set 0x3001:1, save, reboot, verify. Returns the post-reboot rate (Hz) or None."""
    node.sdo.download(0x3001, 1, struct.pack('<I', hz))
    rb = struct.unpack('<I', node.sdo.upload(0x3001, 1))[0]
    if rb != hz:
        print("  WARNING: wrote {} Hz, read back {} Hz -- firmware clamp (needs the".format(hz, rb))
        print("           128 kHz-guard build). Skipping this rate.")
        return None
    node.sdo.download(0x1010, 1, struct.pack('<I', SAVE_MAGIC))
    time.sleep(0.3)
    node.nmt.state = 'RESET'
    time.sleep(2.2)
    try:
        after = struct.unpack('<I', node.sdo.upload(0x3001, 1))[0]
    except Exception:
        after = None
    return after


def _u(node, idx, sub, signed=False, default=None):
    try:
        return int.from_bytes(node.sdo.upload(idx, sub), 'little', signed=signed)
    except Exception:
        return default


def fault_state(node):
    """(faulted, why) from statusword (0x6041 bit3) and error register (0x1001 bit0)."""
    sw = _u(node, 0x6041, 0)
    er = _u(node, 0x1001, 0)
    if sw is None:
        return True, "statusword unreadable (SDO not responding)"
    if sw & 0x08:
        return True, "statusword FAULT bit set (0x{:04X})".format(sw)
    if er:
        return True, "error register 0x1001 = 0x{:02X}".format(er)
    return False, ""


def spin_and_grade(node, enc_res, i_peak, target_cts, dwell, baseline):
    """Spin at target_cts for `dwell` s; return a metrics dict + PASS/FAIL vs baseline."""
    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo['ControlWord'].raw = cw
    node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
    node.sdo['TargetVelocity'].raw = int(target_cts)

    # Grade only the SETTLED window (skip the PROFILE_VEL ramp-up), so a slow ramp
    # isn't misread as "can't hold RPM". settle = 40% of dwell (>=1.2 s).
    settle = max(1.2, 0.4 * dwell)
    RPM, IQ = [], []
    sdo_fails = 0
    faulted, why = False, ""
    t0 = time.time()
    while time.time() - t0 < dwell:
        settled = (time.time() - t0) >= settle
        try:
            rpm = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
            iq = abs(node.sdo['CurrentFeedback'].raw) / 1000.0 * i_peak
            if settled:
                RPM.append(rpm); IQ.append(iq)
        except Exception:
            if settled:
                sdo_fails += 1
        f, w = fault_state(node)
        if f:
            faulted, why = True, w
            break
        time.sleep(0.1)

    node.sdo['TargetVelocity'].raw = 0
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE

    target_rpm = target_cts * 60.0 / enc_res
    m = {
        'rpm': _mean(RPM), 'rpm_sd': _std(RPM), 'iq': _mean(IQ),
        'target_rpm': target_rpm, 'sdo_fails': sdo_fails,
        'faulted': faulted, 'why': why, 'n': len(RPM),
    }

    # --- grade ---
    reasons = []
    if faulted:
        reasons.append("FAULT: " + why)
    if sdo_fails >= 5:
        reasons.append("{} SDO timeouts (ISR starving the main loop)".format(sdo_fails))
    if m['n'] < 3:
        reasons.append("no velocity feedback")
    else:
        if m['rpm'] < 0.85 * target_rpm:
            reasons.append("held only {:.0f}/{:.0f} RPM (<85% of target)".format(m['rpm'], target_rpm))
        if baseline is not None:
            # jitter or current blowing past the clean baseline = commutation breaking down
            if m['rpm_sd'] > max(3.0 * baseline['rpm_sd'], 0.03 * target_rpm + 5):
                reasons.append("RPM jitter {:.0f} vs baseline {:.0f}".format(m['rpm_sd'], baseline['rpm_sd']))
            if baseline['iq'] > 0 and m['iq'] > 1.5 * baseline['iq'] + 50:
                reasons.append("current {:.0f} vs baseline {:.0f} mA (working harder)".format(m['iq'], baseline['iq']))
    m['passed'] = (len(reasons) == 0)
    m['reasons'] = reasons
    return m


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--start', type=int, default=100)
    ap.add_argument('--stop', type=int, default=125)
    ap.add_argument('--step', type=int, default=5)
    ap.add_argument('--rpm', type=float, default=2500.0)
    ap.add_argument('--dwell', type=float, default=4.0)
    ap.add_argument('--cool', type=float, default=3.0)
    ap.add_argument('dev', nargs='?', default='can0')
    ap.add_argument('node', nargs='?', type=int, default=127)
    a = ap.parse_args()
    eds = os.path.join(ROOT, 'puck4.eds')

    net = can_backend.make_network(a.dev, bitrate=1_000_000)
    node = None
    highest_clean = None
    results = []
    try:
        node = net.add_node(a.node, eds)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        i_peak = node.sdo['Calibration']['i_peak'].raw
        target_cts = int(round(a.rpm / 60.0 * enc_res))
        start_hz = a.start * 1000

        print("=" * 62)
        print("PWM CEILING WALK   {}..{} kHz step {}   @ {:.0f} RPM  ({:.1f}s/step)".format(
            a.start, a.stop, a.step, a.rpm, a.dwell))
        print("  overrun shows as: fault / SDO timeout / can't hold RPM / jitter / current spike")
        print("=" * 62)

        baseline = None
        khz = a.start
        while khz <= a.stop:
            hz = khz * 1000
            print("\n[{} kHz] setting + rebooting...".format(khz))
            after = set_pwm(node, hz)
            if after != hz:
                print("  did not persist (got {}) -- stopping walk.".format(after))
                break
            m = spin_and_grade(node, enc_res, i_peak, target_cts, a.dwell, baseline)
            results.append((khz, m))
            tag = "PASS" if m['passed'] else "FAIL"
            print("  {}  RPM {:.0f}/{:.0f} (sd {:.0f})  iq {:.0f} mA".format(
                tag, m['rpm'], m['target_rpm'], m['rpm_sd'], m['iq']))
            if m['passed']:
                highest_clean = khz
                if baseline is None:
                    baseline = m
                    print("       (baseline set: jitter {:.0f} RPM, iq {:.0f} mA)".format(m['rpm_sd'], m['iq']))
            else:
                for r in m['reasons']:
                    print("       - {}".format(r))
                # one clean rate then a failure = we found the wall; stop climbing.
                if baseline is not None:
                    print("  -> ceiling crossed. Stopping.")
                    break
                else:
                    print("  -> even the start rate is unstable; nothing to baseline against. Stopping.")
                    break
            time.sleep(a.cool)
            khz += a.step

        # --- summary ---
        print("\n" + "=" * 62)
        print("RESULT")
        for khz, m in results:
            print("  {:>4} kHz : {}   RPM {:.0f}/{:.0f}  sd {:.0f}  iq {:.0f}".format(
                khz, "PASS" if m['passed'] else "FAIL", m['rpm'], m['target_rpm'], m['rpm_sd'], m['iq']))
        if highest_clean is not None:
            safe = int(highest_clean * 0.90)
            print("  highest CLEAN: {} kHz".format(highest_clean))
            print("  suggested firmware default (10% margin): {} kHz".format(safe))
        else:
            print("  no clean rate in range -- lower --start.")
        print("=" * 62)

        # leave the puck at the highest clean rate (or start) so it's in a good state
        leave = (highest_clean * 1000) if highest_clean else start_hz
        print("Restoring PWM to {} kHz...".format(leave // 1000))
        set_pwm(node, leave)
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                print("Puck idled.")
        except Exception:
            pass
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
