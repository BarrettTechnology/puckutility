#!/usr/bin/env python3
"""CURRENT-GAIN SWEEP -- find the current-loop bandwidth (control_gain) that MAXIMIZES
top speed, so the auto-tuner can be pointed at it.

For this voltage-limited motor we've found LOWER current-loop Ki -> less overmodulation
distortion at the voltage ceiling -> more torque/amp -> higher top speed. This sweeps
control_gain, computes Kp/Ki with the SAME formula the pucktuner app uses (read L/R off
the puck), writes them live to 0x2380, holds max velocity, and reports the gain that
gives the highest settled speed. Original gains restored on exit.

  Kp = 4*pi*bw*zeta*Ldq - Rdq        Ki = (2*pi*bw)^2 * Ldq
  bw = gain * natural_bw             natural_bw = Rdq / (2*pi*Ldq)
  Rdq = R/2   Ldq = (L/2)*PWM_factor(0.8)   zeta = 0.9   (pucktuner defaults)

Usage:  scripts/gain_sweep.py [--start 0.5] [--stop 1.0] [--step 0.1] [--zeta 0.9]
                              [--dwell 2] [--warmup 6] [can] [node]
Safety: spins at the velocity ceiling; watches current -- skips/stops an unstable gain.
        No winding sensor: keep the range tight and let it cool. Drive idled on exit.
"""
import argparse
import math
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import can_backend
from canopen_runner import CLEAR_FAULT, SHUTDOWN, OP_ENABLED, MODE_IDLE, MODE_PROFILE_VEL

PWM_FACTOR = 0.8


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def compute_kp_ki(gain, natural_bw, Ldq, Rdq, zeta):
    bw = gain * natural_bw
    kp = 4.0 * math.pi * bw * zeta * Ldq - Rdq
    if kp < 0:
        kp = 0.0
    ki = (2.0 * math.pi * bw) ** 2 * Ldq
    return bw, kp, ki


def write_gains(node, kp, ki):
    node.sdo.download(0x2380, 1, struct.pack('<f', kp))   # parse handler derives F16 live
    node.sdo.download(0x2380, 2, struct.pack('<f', ki))


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--start', type=float, default=0.5)
    ap.add_argument('--stop', type=float, default=1.0)
    ap.add_argument('--step', type=float, default=0.1)
    ap.add_argument('--zeta', type=float, default=0.9)
    ap.add_argument('--dwell', type=float, default=2.0)
    ap.add_argument('--warmup', type=float, default=6.0)
    ap.add_argument('dev', nargs='?', default='can0')
    ap.add_argument('node', nargs='?', type=int, default=127)
    a = ap.parse_args()
    eds = os.path.join(ROOT, 'puck4.eds')

    net = can_backend.make_network(a.dev, bitrate=1_000_000)
    node = None
    orig = None
    try:
        node = net.add_node(a.node, eds)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        i_peak = node.sdo['Calibration']['i_peak'].raw
        R = int.from_bytes(node.sdo.upload(0x3011, 5), 'little') / 100.0      # 0.01 ohm -> ohm
        L = int.from_bytes(node.sdo.upload(0x3011, 6), 'little') / 1e5        # 0.01 mH -> H
        try:
            max_vel = int(node.sdo['max_velocity'].raw)
        except Exception:
            max_vel = int(round(15000.0 / 60.0 * enc_res))
        orig = (node.sdo.upload(0x2380, 1), node.sdo.upload(0x2380, 2))       # raw bytes, to restore

        Rdq = R / 2.0
        Ldq = (L / 2.0) * PWM_FACTOR
        natural_bw = Rdq / (2.0 * math.pi * Ldq) if Ldq > 0 else 0.0

        gains = []
        g = a.start
        while g <= a.stop + 1e-9:
            gains.append(round(g, 3)); g += a.step

        print("=" * 66)
        print("CURRENT-GAIN SWEEP   R={:.1f}ohm L={:.2f}mH  natural_bw={:.0f}Hz  zeta={:.2f}".format(
            R, L * 1000, natural_bw, a.zeta))
        print("  gain {}..{} step {}  @ max velocity   (lower bw often = higher top speed)".format(
            a.start, a.stop, a.step))
        print("=" * 66)

        for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
            node.sdo['ControlWord'].raw = cw
        node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
        b0, k0, i0 = compute_kp_ki(gains[0], natural_bw, Ldq, Rdq, a.zeta)
        write_gains(node, k0, i0)
        node.sdo['TargetVelocity'].raw = int(max_vel)
        print("Spinning up + letting i2t fold ({:.0f}s)...".format(a.warmup))
        time.sleep(a.warmup)

        print("  {:>5}  {:>7}  {:>8}  {:>9}  {:>9}  {:>8}".format(
            "gain", "bw(Hz)", "Kp", "Ki", "RPM", "|I|mA"))
        results = []
        for gain in gains:
            bw, kp, ki = compute_kp_ki(gain, natural_bw, Ldq, Rdq, a.zeta)
            write_gains(node, kp, ki)
            time.sleep(0.5)
            RPM, IMAG = [], []
            unstable = False
            t0 = time.time()
            while time.time() - t0 < a.dwell:
                try:
                    rpm = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
                    iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                    idc = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                    im = (iq * iq + idc * idc) ** 0.5
                    RPM.append(rpm); IMAG.append(im)
                    if im > 0.95 * i_peak:      # near peak current = unstable at this gain
                        unstable = True; break
                except Exception:
                    pass
                time.sleep(0.05)
            r = (gain, bw, kp, ki, _mean(RPM), _mean(IMAG), unstable)
            results.append(r)
            print("  {:>5.2f}  {:>7.0f}  {:>8.4f}  {:>9.0f}  {:>9.0f}  {:>8.0f}{}".format(
                gain, bw, kp, ki, r[4], r[5], "  UNSTABLE" if unstable else ""))
            if unstable:
                print("       (backing off velocity briefly)")
                node.sdo['TargetVelocity'].raw = 0; time.sleep(0.5)
                node.sdo['TargetVelocity'].raw = int(max_vel); time.sleep(1.0)

        node.sdo['TargetVelocity'].raw = 0
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE

        good = [r for r in results if not r[6] and r[4] > 0]
        if good:
            best = max(good, key=lambda r: r[4])
            print("-" * 66)
            print("  BEST top speed: {:.0f} RPM at control_gain {:.2f}  (bw {:.0f}Hz, Kp {:.4f}, Ki {:.0f})".format(
                best[4], best[0], best[1], best[2], best[3]))
            print("  -> set the tuner's control_gain to {:.2f} (or its floor) to bake this in.".format(best[0]))
            if best[0] == gains[0]:
                print("  NOTE: best is the LOWEST gain tried -- extend --start down to find the real optimum.")
        print("=" * 66)
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if orig is not None:
                    node.sdo.download(0x2380, 1, orig[0])
                    node.sdo.download(0x2380, 2, orig[1])
                    print("Original current gains restored.")
                print("Puck idled.")
        except Exception:
            pass
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
