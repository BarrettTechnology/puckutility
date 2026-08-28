#!/usr/bin/env python3
"""360-spin-test.py -- rotate the OUTPUT exactly one revolution, slowly, to eyeball it.

WHY: the twitch tests move the output by fractions of a degree (a 120-count motor
perturbation is 0.17 deg at the output through a 60.84:1 gearbox) and draw less
current than the bench PSU can resolve. That is invisible on a camera, so this
exists purely to confirm the mechanical/electrical path is alive before trusting
a measurement that looks like nothing happened.

Closed-loop PROFILE_VEL for a computed duration rather than PROFILE_POS: velocity
mode is what every other tool here uses, so the enable path is identical and the
speed is bounded by construction. Position is checked afterwards to confirm the
travel actually happened.

Gearbox regen matters: decelerating an output flywheel pushes energy back and
lifts the bus (measured elsewhere: 51.5 V peaks against a 58 V OVP). Speed and
ramp are deliberately modest, and bus voltage is sampled throughout and reported.

    scripts/360-spin-test.py --node 127 [--gear 60.84] [--secs 20] [--revs 1]
    scripts/360-spin-test.py --node 127 --reverse

SAFE: current-limited by the puck's own i_peak/i2t; always restores
TargetVelocity=0 and IDLE on exit, including on Ctrl-C or any exception.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import can_backend                                    # noqa: E402
from canopen_runner import (MODE_IDLE, MODE_PROFILE_VEL,               # noqa: E402
                            CLEAR_FAULT, SHUTDOWN, OP_ENABLED)

EDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "puck4.eds")


def bus_v(node):
    try:
        return int(node.sdo[0x3000][1].raw) / 10.0
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--can", default="can0")
    ap.add_argument("--node", type=int, default=127)
    ap.add_argument("--gear", type=float, default=60.84, help="gearbox reduction")
    ap.add_argument("--revs", type=float, default=1.0, help="OUTPUT revolutions")
    ap.add_argument("--secs", type=float, default=20.0, help="how long to take")
    ap.add_argument("--reverse", action="store_true")
    args = ap.parse_args()

    net = can_backend.make_network(args.can, bitrate=1_000_000)
    node = net.add_node(args.node, EDS)
    node.sdo.RESPONSE_TIMEOUT = 1.0

    try:
        enc = int(node.sdo["EncoderConfig"]["Resolution"].raw)
        i_peak = int(node.sdo["Calibration"]["i_peak"].raw)
        start = int(node.sdo["PositionFeedback"].raw)

        motor_revs = args.revs * args.gear
        total_cts = motor_revs * enc
        cts_per_s = total_cts / args.secs
        sign = -1 if args.reverse else 1

        print(f"node {args.node}  enc_res={enc}  i_peak={i_peak} mA  gear={args.gear}:1")
        print(f"  target      : {args.revs} OUTPUT rev = {motor_revs:.1f} motor rev "
              f"= {total_cts:,.0f} cts")
        print(f"  speed       : {cts_per_s:,.0f} cts/s = {cts_per_s/enc*60:.0f} RPM motor "
              f"= {cts_per_s/enc*60/args.gear:.2f} RPM output")
        print(f"  duration    : {args.secs:.0f} s   direction: "
              f"{'REVERSE' if args.reverse else 'forward'}")
        print(f"  start pos   : {start:,}   bus {bus_v(node)} V")
        print()

        node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
            node.sdo["ControlWord"].raw = cw
        node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
        node.sdo["TargetVelocity"].raw = int(sign * cts_per_s)

        t0 = time.time()
        vmax = 0.0
        while time.time() - t0 < args.secs:
            time.sleep(0.5)
            pos = int(node.sdo["PositionFeedback"].raw)
            v = bus_v(node)
            if v and v > vmax:
                vmax = v
            done = abs(pos - start) / total_cts * 100
            print(f"  t={time.time()-t0:5.1f}s  pos={pos:>10,}  "
                  f"{done:5.1f}% of one output rev   bus {v} V")

        node.sdo["TargetVelocity"].raw = 0
        time.sleep(1.0)
        end = int(node.sdo["PositionFeedback"].raw)
        moved = abs(end - start)
        print()
        print(f"  moved       : {moved:,} cts = {moved/enc:.2f} motor rev "
              f"= {moved/enc/args.gear:.3f} OUTPUT rev")
        print(f"  peak bus    : {vmax} V   (OVP trips at 58.0 V)")
        print(f"  verdict     : {'MOVED' if moved > total_cts*0.5 else 'DID NOT MOVE as expected'}")
    finally:
        try:
            node.sdo["TargetVelocity"].raw = 0
            node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            print("  restored    : TargetVelocity=0, IDLE")
        except Exception:
            pass
        net.disconnect()


if __name__ == "__main__":
    sys.exit(main())
