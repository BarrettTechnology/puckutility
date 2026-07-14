#!/usr/bin/env python3
"""spin_check.py -- encoder-compensation dynamic sanity check (comp ON vs OFF).

Encoder correction is applied to the COMMUTATION angle, so a steep correction can modulate commutation
gain and destabilise the CLOSED-LOOP hold (the classic "0-cmd hold runaway"). The in-app cal's retest is
open-loop/stepped and can't see that; this drives the motor CLOSED-LOOP and compares. enc-comp fades out
at speed, so its risk AND benefit both live at HOLD / low speed -- this stays there on purpose.

For a set of low speeds (0 = the 0-cmd hold, then a short crawl ladder up to --max-rpm), it commands the
velocity comp-ON then comp-OFF and reports, per speed:
  * velocity oscillation  -- hold pk-pk / crawl rms ripple (cts/s). ON >> OFF at the hold = HUNTING.
  * |I| (current)         -- lower ON than OFF at the same speed = the correction is doing real work (less
                             current wasted fighting a mis-commutated angle) = the BENEFIT.

  scripts/spin_check.py                        # can0, node 127, hold + 4/8/12 RPM
  scripts/spin_check.py --node 1 --max-rpm 15 --steps 3
  scripts/spin_check.py --secs 3.0             # longer dwell per point (steadier stats)

Uses the same drive-script setup as top_speed_vitals.py (can_backend + puck4.eds -> named DS402 objects).
SAFE: current-limited by the puck's own i_peak/i2t; stops on any drive fault; always restores
TargetVelocity=0 + IDLE and leaves the compensation Active flag (0x3027:1) in its ORIGINAL state on exit.
It ONLY reads/commands velocity + toggles 0x3027:1 -- it never writes the correction table.
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
SW_FAULT = 0x08          # StatusWord fault bit


def _imag_mA(node, i_peak):
    _id = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
    _iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
    return (_id * _id + _iq * _iq) ** 0.5


def _sample(node, i_peak, vcmd, secs):
    """Command vcmd (cts/s), settle, then sample velocity + |I| for `secs`. Returns (velocity_list, imax),
    or (None, imax) if the drive faulted mid-sample (treated as unstable)."""
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


def _osc(vs):
    """Velocity oscillation metric: max of pk-pk and mean-removed rms, so a limit cycle shows either way."""
    if not vs:
        return 0.0
    m = sum(vs) / len(vs)
    rms = (sum((x - m) ** 2 for x in vs) / len(vs)) ** 0.5
    return max(max(vs) - min(vs), rms)


def _run_speed(node, i_peak, vcmd, secs):
    vs, imax = _sample(node, i_peak, vcmd, secs)
    if vs is None:
        return None
    return {'osc': _osc(vs), 'imax': imax}


def main():
    ap = argparse.ArgumentParser(description="Encoder-comp dynamic check: closed-loop hold+crawl, ON vs OFF.")
    ap.add_argument('--can', default='can0', help='SocketCAN interface (default: can0)')
    ap.add_argument('--node', type=int, default=127, help='CANopen node id (default: 127)')
    ap.add_argument('--max-rpm', type=float, default=12.0, help='top crawl speed, RPM (default: 12)')
    ap.add_argument('--steps', type=int, default=3, help='number of non-zero crawl speeds (default: 3)')
    ap.add_argument('--secs', type=float, default=2.2, help='dwell/sample seconds per speed (default: 2.2)')
    args = ap.parse_args()

    net = can_backend.make_network(args.can, bitrate=1_000_000)
    node = None
    orig_comp = None
    try:
        node = net.add_node(args.node, EDS)

        i_peak  = node.sdo['Calibration']['i_peak'].raw
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        orig_comp = int(node.sdo[0x3027][1].raw)
        print("Node {}  enc_res={}  i_peak={} mA  (comp currently {})".format(
            args.node, enc_res, i_peak, "ON" if orig_comp else "OFF"))

        rpm_to_cts = enc_res / 60.0
        speeds = [0.0] + [args.max_rpm * (i + 1) / args.steps for i in range(args.steps)]

        print("\nEnabling closed-loop velocity mode (PROFILE_VEL)...")
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE
        for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
            node.sdo['ControlWord'].raw = cw
        node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL

        print("\n{:>8}  {:>20}  {:>20}  {}".format(
            "speed", "comp ON (osc||I|)", "comp OFF (osc||I|)", "verdict"))
        print("  " + "-" * 72)
        rows = []
        for rpm in speeds:
            vcmd = rpm * rpm_to_cts
            node.sdo[0x3027][1].raw = 1
            on = _run_speed(node, i_peak, vcmd, args.secs)
            node.sdo[0x3027][1].raw = 0
            off = _run_speed(node, i_peak, vcmd, args.secs)
            tag = "hold" if rpm == 0 else "{:.0f} RPM".format(rpm)
            if on is None or off is None:
                print("  {:>8}  drive FAULTED during {} — stopping".format(
                    tag, "comp ON" if on is None else "comp OFF"))
                break
            floor = max(30.0, 0.02 * (vcmd or 1.0))
            hunts = on['osc'] > floor and on['osc'] > 2.0 * max(off['osc'], floor)
            better_i = on['imax'] < off['imax'] - 5.0
            verdict = "HUNTS(ON)" if hunts else ("stable" + ("  +benefit(↓I)" if better_i else ""))
            print("  {:>8}  {:>9.0f} | {:>6.0f}  {:>9.0f} | {:>6.0f}  {}".format(
                tag, on['osc'], on['imax'], off['osc'], off['imax'], verdict))
            rows.append((on, off, hunts))

        any_hunt = any(r[2] for r in rows)
        print("  " + "-" * 72)
        if any_hunt:
            print("VERDICT: comp HUNTS at ≥1 speed — the correction destabilises the hold. Do NOT trust it;")
            print("         re-run the cal (the in-app gate should also catch + revert this).")
        elif rows:
            print("VERDICT: stable at every speed. " + (
                "Lower |I| with comp ON confirms the correction is doing real work."
                if any(r[0]['imax'] < r[1]['imax'] - 5.0 for r in rows)
                else "No hunting; |I| benefit inconclusive at these speeds."))
        else:
            print("VERDICT: no data (faulted immediately).")
        return 0 if (rows and not any_hunt) else 1
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if orig_comp is not None:                    # restore the comp flag to how we found it
                    node.sdo[0x3027][1].raw = orig_comp
                    print("\nRestored: TargetVelocity=0, IDLE, comp {} (as found).".format(
                        "ON" if orig_comp else "OFF"))
        except Exception as e:
            print("\n(cleanup warning: {} — verify the puck is IDLE)".format(e))
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
