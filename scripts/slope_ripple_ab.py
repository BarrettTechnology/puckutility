#!/usr/bin/env python3
"""slope_ripple_ab.py -- does the current-sense SLOPE + OFFSET correction reduce low-speed ripple?

Sibling of spin_check.py, but instead of toggling encoder-comp it toggles the current-sense
corrections: the SLOPE coefficients (kA/kB, 0x3008:7 / 0x3009:7) and the drive-gated OFFSET
(a0/b0, 0x3008:8 / 0x3009:8, if the firmware has it). Both are a 1x-electrical current-sense error
-> once-per-mech-rev torque ripple felt as low-speed "cogging", which is exactly what these correct.

For hold + a short crawl ladder it measures velocity oscillation and |I| with the correction ON
(the calibrated values) vs OFF (coeffs zeroed live), so you can see directly whether the slope/offset
calibration is what's carrying the low-speed smoothness -- i.e. whether the "settling" symptom was
really the current-sense offset all along.

  scripts/slope_ripple_ab.py --node 127
  scripts/slope_ripple_ab.py --node 1 --max-rpm 15 --steps 3

SAFE: current-limited by the puck's own i_peak/i2t; stops on drive fault; ALWAYS restores the original
slope/offset coefficients (live only -- never writes NV) + TargetVelocity=0 + IDLE on exit.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend
from canopen_runner import CLEAR_FAULT, SHUTDOWN, OP_ENABLED, MODE_IDLE, MODE_PROFILE_VEL

EDS      = os.path.join(ROOT, 'puck4.eds')
SW_FAULT = 0x08


def _imag_mA(node, i_peak):
    _id = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
    _iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
    return (_id * _id + _iq * _iq) ** 0.5


def _osc(vs):
    if not vs:
        return 0.0
    m = sum(vs) / len(vs)
    rms = (sum((x - m) ** 2 for x in vs) / len(vs)) ** 0.5
    return max(max(vs) - min(vs), rms)


def _sample(node, i_peak, vcmd, secs):
    node.sdo['TargetVelocity'].raw = int(vcmd)
    time.sleep(0.6)
    vs = []; imax = 0.0; t0 = time.time()
    while time.time() - t0 < secs:
        vs.append(node.sdo['VelocityFeedback'].raw)
        imax = max(imax, _imag_mA(node, i_peak))
        if node.sdo['StatusWord'].raw & SW_FAULT:
            return None, imax
        time.sleep(0.01)
    return vs, imax


def _run_speed(node, i_peak, vcmd, secs):
    vs, imax = _sample(node, i_peak, vcmd, secs)
    return None if vs is None else {'osc': _osc(vs), 'imax': imax}


def _i16(node, idx, sub):
    """Raw signed-16 read of an OD entry (works even if the EDS lacks a name)."""
    return int.from_bytes(node.sdo.upload(idx, sub), 'little', signed=True)


def _set_i16(node, idx, sub, val):
    node.sdo.download(idx, sub, int(val).to_bytes(2, 'little', signed=True))


def main():
    ap = argparse.ArgumentParser(description="Current-sense slope+offset ripple A/B (ON vs zeroed).")
    ap.add_argument('--can', default='can0')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--max-rpm', type=float, default=12.0)
    ap.add_argument('--steps', type=int, default=3)
    ap.add_argument('--secs', type=float, default=2.2)
    args = ap.parse_args()

    net = can_backend.make_network(args.can, bitrate=1_000_000)
    node = None
    orig = None          # (slopeA, slopeB, offA, offB, has_off)
    try:
        node = net.add_node(args.node, EDS)
        i_peak  = node.sdo['Calibration']['i_peak'].raw
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw

        sA = _i16(node, 0x3008, 7); sB = _i16(node, 0x3009, 7)
        has_off = True
        try:
            oA = _i16(node, 0x3008, 8); oB = _i16(node, 0x3009, 8)
        except Exception:
            has_off = False; oA = oB = 0
        orig = (sA, sB, oA, oB, has_off)
        print("Node {}  enc_res={}  i_peak={} mA".format(args.node, enc_res, i_peak))
        print("  slope kA/kB (Q4.12) = {}/{}   offset a0/b0 (mA) = {}{}".format(
            sA, sB, oA if has_off else "-", "/" + str(oB) if has_off else "",
            ))
        print("  drive-gated offset register: {}".format("present" if has_off else "ABSENT (older fw)"))

        def _set_correction(on):
            if on:
                _set_i16(node, 0x3008, 7, sA); _set_i16(node, 0x3009, 7, sB)
                if has_off:
                    _set_i16(node, 0x3008, 8, oA); _set_i16(node, 0x3009, 8, oB)
            else:
                _set_i16(node, 0x3008, 7, 0); _set_i16(node, 0x3009, 7, 0)
                if has_off:
                    _set_i16(node, 0x3008, 8, 0); _set_i16(node, 0x3009, 8, 0)

        rpm_to_cts = enc_res / 60.0
        speeds = [0.0] + [args.max_rpm * (i + 1) / args.steps for i in range(args.steps)]

        print("\nEnabling closed-loop velocity mode (PROFILE_VEL)...")
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE
        for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
            node.sdo['ControlWord'].raw = cw
        node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL

        print("\n{:>8}  {:>22}  {:>22}  {}".format(
            "speed", "corr ON (osc||I|)", "corr OFF (osc||I|)", "verdict"))
        print("  " + "-" * 76)
        rows = []
        for rpm in speeds:
            vcmd = rpm * rpm_to_cts
            _set_correction(True)
            on = _run_speed(node, i_peak, vcmd, args.secs)
            _set_correction(False)
            off = _run_speed(node, i_peak, vcmd, args.secs)
            tag = "hold" if rpm == 0 else "{:.0f} RPM".format(rpm)
            if on is None or off is None:
                print("  {:>8}  drive FAULTED during {} — stopping".format(
                    tag, "corr ON" if on is None else "corr OFF"))
                break
            # correction HELPS if it lowers |I| and/or osc
            di = off['imax'] - on['imax']
            do = off['osc'] - on['osc']
            verdict = ("correction HELPS (↓I {:+.0f}mA, ↓osc {:+.0f})".format(di, do)
                       if (di > 5.0 or do > 30.0) else
                       ("~no effect" if abs(di) <= 5.0 and abs(do) <= 30.0 else
                        "correction HURTS (↑I/↑osc)"))
            print("  {:>8}  {:>11.0f} | {:>6.0f}  {:>11.0f} | {:>6.0f}  {}".format(
                tag, on['osc'], on['imax'], off['osc'], off['imax'], verdict))
            rows.append((on, off))

        print("  " + "-" * 76)
        if rows:
            di_avg = sum(o['imax'] - n['imax'] for n, o in rows) / len(rows)
            print("VERDICT: mean |I| change (OFF - ON) = {:+.0f} mA.  {}".format(
                di_avg,
                "Correction ON runs LEANER -> the slope/offset cal IS doing real work (this is the fix)."
                if di_avg > 5.0 else
                "Little/no |I| difference -> slope/offset is not the active lever here (residual ripple "
                "is other/normal; nothing to chase)."))
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if orig is not None:                      # restore the calibrated coeffs (live)
                    _set_i16(node, 0x3008, 7, orig[0]); _set_i16(node, 0x3009, 7, orig[1])
                    if orig[4]:
                        _set_i16(node, 0x3008, 8, orig[2]); _set_i16(node, 0x3009, 8, orig[3])
                    print("\nRestored: slope/offset coeffs, TargetVelocity=0, IDLE.")
        except Exception as e:
            print("\n(cleanup warning: {} — verify the puck is IDLE + coeffs restored)".format(e))
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
