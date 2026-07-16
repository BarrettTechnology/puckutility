#!/usr/bin/env python3
"""LAG SWEEP -- find the LagFactor (0x3013:5) that maximizes TOP SPEED, with NO 256 cap.

LagFactor is a speed-proportional commutation ADVANCE: enc.est = raw + enc_inc*lag/256.
It's not a delay fix -- it FIELD-WEAKENS the machine (advances the angle -> injects -id ->
cuts effective back-EMF -> raises the voltage-limited top speed). The auto-cal
(calibrate_enclag) stops at 256 (=1.0 elec cycle), but on a fast motor the FW optimum is
HIGHER. This sweeps whatever range you give it (incl. >256), holds max velocity, and
reports the lag that gives the highest settled speed -- for the gains currently loaded.

Usage:  scripts/lag_sweep.py [--start 260] [--stop 380] [--step 10] [--dwell 2]
                             [--warmup 6] [--margin 8] [--save] [can] [node]
Safety: spins at the velocity ceiling for ~warmup + N*dwell seconds. No winding sensor --
        keep the range tight and let it cool. Drive idled on exit / Ctrl-C.
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

SAVE_MAGIC = 0x65766173  # 'save'


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def set_lag(node, lag):
    node.sdo.download(0x3013, 5, struct.pack('<H', int(lag)))  # applies live


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--start', type=int, default=260)
    ap.add_argument('--stop', type=int, default=380)
    ap.add_argument('--step', type=int, default=10)
    ap.add_argument('--dwell', type=float, default=2.0, help='seconds held per lag point')
    ap.add_argument('--warmup', type=float, default=6.0, help='seconds to let i2t fold before sweeping')
    ap.add_argument('--margin', type=int, default=8, help='back off this many counts below the peak lag')
    ap.add_argument('--save', action='store_true', help='save the chosen lag to NV (else just applied live)')
    ap.add_argument('dev', nargs='?', default='can0')
    ap.add_argument('node', nargs='?', type=int, default=127)
    a = ap.parse_args()
    eds = os.path.join(ROOT, 'puck4.eds')

    net = can_backend.make_network(a.dev, bitrate=1_000_000)
    node = None
    orig_lag = None
    try:
        node = net.add_node(a.node, eds)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        i_peak = node.sdo['Calibration']['i_peak'].raw
        try:
            orig_lag = struct.unpack('<H', node.sdo.upload(0x3013, 5))[0]
        except Exception:
            orig_lag = None
        try:
            max_vel = int(node.sdo['max_velocity'].raw)
        except Exception:
            max_vel = int(round(15000.0 / 60.0 * enc_res))

        lags = list(range(a.start, a.stop + 1, a.step))
        print("=" * 60)
        print("LAG SWEEP  {}..{} step {}  @ max velocity ({} cts/s)".format(
            a.start, a.stop, a.step, max_vel))
        print("  (auto-cal caps at 256; this does NOT -- LagFactor = field-weakening)")
        print("=" * 60)

        for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
            node.sdo['ControlWord'].raw = cw
        node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
        set_lag(node, a.start)
        node.sdo['TargetVelocity'].raw = int(max_vel)
        print("Spinning up + letting i2t fold ({:.0f}s)...".format(a.warmup))
        time.sleep(a.warmup)

        print("  {:>5}  {:>9}  {:>8}".format("lag", "RPM", "|I| mA"))
        results = []
        for lag in lags:
            set_lag(node, lag)
            time.sleep(0.4)  # let the new advance settle
            RPM, IMAG = [], []
            t0 = time.time()
            while time.time() - t0 < a.dwell:
                try:
                    rpm = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
                    iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                    idc = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                    RPM.append(rpm); IMAG.append((iq * iq + idc * idc) ** 0.5)
                except Exception:
                    pass
                time.sleep(0.05)
            r = (lag, _mean(RPM), _mean(IMAG))
            results.append(r)
            print("  {:>5}  {:>9.0f}  {:>8.0f}".format(*r))

        node.sdo['TargetVelocity'].raw = 0
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE

        if not results:
            print("No data.")
            return 1
        best = max(results, key=lambda r: r[1])
        chosen = max(a.start, best[0] - a.margin)
        rising = results[-1][1] >= results[0][1] and best[0] == lags[-1]
        print("-" * 60)
        print("  peak speed {:.0f} RPM at lag {}".format(best[1], best[0]))
        if rising:
            print("  NOTE: still RISING at the top of the range -- widen --stop to find the true peak.")
        print("  -> chosen lag: {} (peak {} - {} margin)".format(chosen, best[0], a.margin))
        set_lag(node, chosen)
        if a.save:
            node.sdo.download(0x1010, 1, struct.pack('<I', SAVE_MAGIC))
            time.sleep(0.3)
            print("  saved LagFactor={} to NV.".format(chosen))
        else:
            print("  applied live (not saved). Re-run with --save to persist.")
        print("=" * 60)
        orig_lag = None  # leave the chosen lag applied
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if orig_lag is not None:
                    set_lag(node, orig_lag)
                    print("Lag restored to {}.".format(orig_lag))
                print("Puck idled.")
        except Exception:
            pass
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
