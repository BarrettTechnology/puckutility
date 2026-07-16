#!/usr/bin/env python3
"""POSITION / VELOCITY NOISE LOG -- quantify encoder feedback jitter at a hold.

Torque control uses the encoder ANGLE for commutation, and velocity = d(pos)/dt
amplifies position noise -- so a noisy encoder shows up as both torque ripple and
velocity-loop chatter at low speed. This holds a speed (0 = position hold) and
logs position (0x6064), velocity (0x606C) and iq (CurrentFeedback), then reports
the jitter (std) of each so 'is the encoder noisy' is a number.

Usage:  scripts/pos_noise_log.py [--rpm 0] [--dwell 5] [can] [node]
        scripts/pos_noise_log.py --rpm 0            # position hold, measure jitter
        scripts/pos_noise_log.py --rpm 25           # slow spin, measure vel/iq noise
Safety: brief low-speed hold; drive idled on exit / Ctrl-C.

NOTE: SDO polling is ~100-200 Hz, so this captures the low-frequency envelope of
the noise, not per-control-cycle detail -- enough to confirm/deny a noisy encoder.
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


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _std(a):
    if len(a) < 2:
        return 0.0
    m = _mean(a)
    return (sum((x - m) ** 2 for x in a) / len(a)) ** 0.5


def _detrend_std(t, y):
    """std of y after removing a linear fit vs t (so a ramp doesn't inflate jitter)."""
    n = len(y)
    if n < 3:
        return 0.0
    tm, ym = _mean(t), _mean(y)
    den = sum((tt - tm) ** 2 for tt in t)
    slope = (sum((t[i] - tm) * (y[i] - ym) for i in range(n)) / den) if den > 0 else 0.0
    resid = [y[i] - (ym + slope * (t[i] - tm)) for i in range(n)]
    return _std(resid)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--rpm', type=float, default=0.0, help='hold speed (0 = position hold)')
    ap.add_argument('--idle', action='store_true',
                    help='drive OFF (no torque) -- measures PURE encoder noise vs the servo-held case')
    ap.add_argument('--encvel-fc', type=int, metavar='HZ', dest='encvel_fc',
                    help='set encoder-velocity filter 0x2100:1 (Hz), save+reboot, THEN run the test '
                         '(applies via motion_init; lower = smoother but more lag). Left applied on exit.')
    ap.add_argument('--dwell', type=float, default=5.0)
    ap.add_argument('dev', nargs='?', default='can0')
    ap.add_argument('node', nargs='?', type=int, default=127)
    a = ap.parse_args()
    eds = os.path.join(ROOT, 'puck4.eds')

    net = can_backend.make_network(a.dev, bitrate=1_000_000)
    node = None
    try:
        node = net.add_node(a.node, eds)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        i_peak = node.sdo['Calibration']['i_peak'].raw
        target = int(round(a.rpm / 60.0 * enc_res))

        if a.encvel_fc is not None:
            was = struct.unpack('<H', node.sdo.upload(0x2100, 1))[0]
            node.sdo.download(0x2100, 1, struct.pack('<H', a.encvel_fc))
            node.sdo.download(0x1010, 1, struct.pack('<I', 0x65766173))  # 'save' all
            time.sleep(0.3)
            node.nmt.state = 'RESET'
            time.sleep(2.2)
            try:
                now = struct.unpack('<H', node.sdo.upload(0x2100, 1))[0]
            except Exception:
                now = None
            print("encvel_fc (0x2100:1): {} -> {} Hz (saved, rebooted).".format(was, now))
            enc_res = node.sdo['EncoderConfig']['Resolution'].raw
            i_peak = node.sdo['Calibration']['i_peak'].raw

        if a.idle:
            node.sdo['SetModeOfOperation'].raw = MODE_IDLE   # drive off -> no torque
            print("Drive OFF (idle, no torque).  Logging {:.1f}s -- PURE encoder noise.".format(a.dwell))
            print("  (keep the shaft undisturbed; position jitter here is the sensor floor)")
        else:
            for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
                node.sdo['ControlWord'].raw = cw
            node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
            node.sdo['TargetVelocity'].raw = int(target)
            print("Holding {:.0f} RPM ({} cts/s).  Logging {:.1f}s...".format(a.rpm, target, a.dwell))
            if a.rpm == 0:
                print("  (position hold -- you can also apply load by hand during the window)")

        T, POS, VEL, IQ = [], [], [], []
        t0 = time.time()
        while time.time() - t0 < a.dwell:
            try:
                pos = int.from_bytes(node.sdo.upload(0x6064, 0), 'little', signed=True)
                vel = int.from_bytes(node.sdo.upload(0x606C, 0), 'little', signed=True)
                iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
            except Exception:
                continue
            T.append(time.time() - t0); POS.append(pos); VEL.append(vel); IQ.append(iq)
            time.sleep(0.005)

        node.sdo['TargetVelocity'].raw = 0
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE

        if len(POS) < 3:
            print("Not enough samples.")
            return 1

        pos_jit = _detrend_std(T, POS)          # counts, ramp removed
        vel_std = _std(VEL)                      # cts/s
        vel_std_rpm = vel_std * 60.0 / enc_res
        iq_std = _std(IQ)                        # mA
        iq_pp = (max(IQ) - min(IQ))

        print("=" * 58)
        print("NOISE @ {:.0f} RPM   ({} samples, {:.1f}s, ~{:.0f} Hz poll)".format(
            a.rpm, len(POS), T[-1], len(POS) / T[-1] if T[-1] else 0))
        print("-" * 58)
        print("  position jitter (detrended):  {:6.2f} cts   ({:.3f}% of 1 rev)".format(
            pos_jit, 100.0 * pos_jit / enc_res))
        print("  velocity noise (std):         {:6.0f} cts/s  ({:.1f} RPM)".format(
            vel_std, vel_std_rpm))
        print("  iq ripple (std / pk-pk):      {:6.1f} / {:.1f} mA".format(iq_std, iq_pp))
        print("-" * 58)
        # interpretation
        if pos_jit > 1.5:
            print("  => POSITION is noisy ({:.1f} cts jitter). That rides straight into the".format(pos_jit))
            print("     commutation angle (torque ripple) AND gets amplified by d/dt into velocity.")
        else:
            print("  => position looks clean ({:.1f} cts). Weirdness likely elsewhere (deadtime,".format(pos_jit))
            print("     cogging, or velocity-quantization from the single-cycle dx estimate).")
        if vel_std_rpm > max(2.0, 0.2 * abs(a.rpm) + 1):
            print("  => VELOCITY estimate is chattering ({:.1f} RPM std) -- the loop reacts to it.".format(vel_std_rpm))
        print("=" * 58)
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
