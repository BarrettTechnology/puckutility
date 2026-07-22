#!/usr/bin/env python3
"""settling_behavioral.py -- pick MaxSettlingTime by the SYMPTOM (low-speed ripple), not the sense.

Every attempt to read "best settling" off the current sense failed (held-angle conduction artifact,
below-noise spin current, logger-drain stalls). So this measures the thing that actually matters --
the motor's low-speed velocity ripple + current -- as a function of MaxSettlingTime, with a FAIR
comparison (bias is settling-specific, so it is re-measured & written at each step). No logger, so no
drain artifacts.

GOAL: find the EARLIEST MaxSettlingTime past the switching-ring floor -> lowest value = MAX duty cycle.
Below the floor you sample IN the ring: the measured current reads INFLATED and low-speed ripple rises
(both reproducible). Above it, mean|I| plateaus and settling stops mattering. The floor is that knee.

Per settling value (fine grid):
  1. set MaxSettlingTime (live)
  2. RE-CAL bias: energise at zero current, average raw alpha/beta, write 0x3008:3 / 0x3009:3
     (Q12.4 = raw*16) -- so each settling is judged with its OWN correct bias, not a stale one
  3. measure mean|I| + velocity ripple over hold + a short crawl ladder
PICK = the RING FLOOR: the EARLIEST settling where mean|I| has collapsed to its plateau (ring cleared).
mean|I| is the reproducible signal; ripple confirms. --passes checks the floor repeats run-to-run.

SAFE: current-limited by i_peak/i2t; stops on drive fault; ALWAYS restores original bias + settling
(live only, never NV) + TargetVelocity=0 + IDLE on exit.

  scripts/settling_behavioral.py --node 127
  scripts/settling_behavioral.py --node 1 --settles 100,300,600,900 --max-rpm 15 --steps 3
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

EDS = os.path.join(ROOT, 'puck4.eds')
MODE_PHASE_VOLTAGE_ANGLE = 12
SW_FAULT = 0x08
IDX_SETTLE = (0x3001, 5)


def _imag(node, i_peak):
    _id = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
    _iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
    return (_id * _id + _iq * _iq) ** 0.5


def _osc(vs):
    if not vs:
        return 0.0
    m = sum(vs) / len(vs)
    return max(max(vs) - min(vs), (sum((x - m) ** 2 for x in vs) / len(vs)) ** 0.5)


def _enable(node, mode):
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo['ControlWord'].raw = cw
    node.sdo['SetModeOfOperation'].raw = mode


def _recal_bias(node, n=40):
    """Energise at zero current and average raw alpha/beta -> write bias (0x3008:3 / 0x3009:3, Q12.4).
    Returns (bias_a_od, bias_b_od) that were written."""
    _enable(node, MODE_PHASE_VOLTAGE_ANGLE)
    node.sdo['Theta_e'].raw = 0
    node.sdo['Motor']['ud'].raw = 0
    time.sleep(0.2)
    ra = []; rb = []
    for _ in range(n):
        ra.append(node.sdo[0x3008][1].raw); rb.append(node.sdo[0x3009][1].raw)
        time.sleep(0.003)
    node.sdo['Motor']['ud'].raw = 0
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    ba = int(round(sum(ra) / len(ra) * 16)); bb = int(round(sum(rb) / len(rb) * 16))
    node.sdo[0x3008][3].raw = ba; node.sdo[0x3009][3].raw = bb
    return ba, bb


def _ripple(node, i_peak, rpm_to_cts, speeds, secs):
    """Velocity osc + |I| over the speed ladder. Returns (total_osc, mean_i, faulted)."""
    _enable(node, MODE_PROFILE_VEL)
    oscs = []; imaxes = []
    for rpm in speeds:
        node.sdo['TargetVelocity'].raw = int(rpm * rpm_to_cts)
        time.sleep(0.6)
        vs = []; imax = 0.0; t0 = time.time()
        while time.time() - t0 < secs:
            vs.append(node.sdo['VelocityFeedback'].raw)
            imax = max(imax, _imag(node, i_peak))
            if node.sdo['StatusWord'].raw & SW_FAULT:
                node.sdo['TargetVelocity'].raw = 0; node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                return 0.0, 0.0, True
            time.sleep(0.01)
        oscs.append(_osc(vs)); imaxes.append(imax)
    node.sdo['TargetVelocity'].raw = 0
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    return sum(oscs), (sum(imaxes) / len(imaxes) if imaxes else 0.0), False


def main():
    ap = argparse.ArgumentParser(description="Behavioral MaxSettlingTime pick (ripple vs settling).")
    ap.add_argument('--can', default='can0')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--settles', default='50,100,150,200,250,300,350,400',
                    help='MaxSettlingTime ns values (fine grid to pin the ring floor for max duty)')
    ap.add_argument('--max-rpm', type=float, default=12.0)
    ap.add_argument('--steps', type=int, default=2, help='crawl speeds above hold')
    ap.add_argument('--secs', type=float, default=2.0, help='sample seconds per speed')
    ap.add_argument('--passes', type=int, default=1)
    ap.add_argument('--floor-tol', type=float, default=0.10,
                    help='mean|I| within this frac of the plateau counts as "ring cleared" (default 0.10)')
    ap.add_argument('--set', action='store_true', help='apply the winning settling live')
    ap.add_argument('--save', action='store_true', help='persist winner to NV (implies --set)')
    args = ap.parse_args()

    settles = [int(s) for s in args.settles.split(',') if s.strip()]
    net = can_backend.make_network(args.can, bitrate=1_000_000)
    node = None
    orig_settle = orig_bias = None
    try:
        node = net.add_node(args.node, EDS)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        i_peak  = node.sdo['Calibration']['i_peak'].raw
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        rpm_to_cts = enc_res / 60.0
        speeds = [0.0] + [args.max_rpm * (i + 1) / args.steps for i in range(args.steps)]
        orig_settle = node.sdo[IDX_SETTLE[0]][IDX_SETTLE[1]].raw
        orig_bias = (node.sdo[0x3008][3].raw, node.sdo[0x3009][3].raw)
        print("Node {}  enc_res={}  i_peak={} mA  orig settle={} ns  orig bias={}".format(
            args.node, enc_res, i_peak, orig_settle, orig_bias))
        # SAFETY GATE: bias (0x3008:3 / 0x3009:3) is Q12.4 = raw*16, so a healthy zero-current bias is
        # ~32768 (raw ~2048). A bias far from that (e.g. raw counts stored directly) makes the firmware
        # compute a huge phantom current -> the loop commands garbage current -> the motor TWITCHES.
        # Refuse to DRIVE such a puck: it is uncalibrated. Do NOT touch bias / do not spin.
        if not all(24000 <= b <= 42000 for b in orig_bias):
            print("\n!! ABORT: bias {} is INVALID (expected Q12.4 ~32768 = raw*16). This puck is NOT "
                  "calibrated -- driving it would command garbage current and twitch the motor. Run a "
                  "full current-sense calibration on this motor FIRST, then re-run the settling test."
                  .format(orig_bias))
            orig_bias = None            # nothing valid to restore; leave the puck untouched
            return 2
        print("Sweeping MaxSettlingTime = {} ns  x speeds {} RPM  (bias re-cal'd per step)\n".format(
            settles, [round(s) for s in speeds]))

        # RANDOMIZED order across all passes: each settle is measured `passes` times, but the whole
        # sequence is shuffled so THERMAL drift (a monotone time trend as the motor heats) does NOT
        # align with any settling value. A fixed seed keeps the order reproducible run-to-run.
        import random
        schedule = list(settles) * args.passes
        random.Random(20260715).shuffle(schedule)
        print("Randomized schedule: {} measurements ({} settles x {} passes) -- de-correlates heating "
              "from settling.\n".format(len(schedule), len(settles), args.passes))
        print("{:>4} {:>8} {:>12} {:>9} {:>8}".format("#", "settle", "bias a/b", "totosc", "mean|I|"))
        meas = []   # (settle, osc, mi, tidx)
        for tidx, st in enumerate(schedule):
            node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            node.sdo[IDX_SETTLE[0]][IDX_SETTLE[1]].raw = st
            ba, bb = _recal_bias(node)
            osc, mi, fault = _ripple(node, i_peak, rpm_to_cts, speeds, args.secs)
            if fault:
                print("  FAULT during ripple measure -- stopping"); break
            meas.append((st, osc, mi, tidx))
            print("{:>4} {:>8} {:>12} {:>9.0f} {:>8.0f}".format(
                tidx + 1, st, "{}/{}".format(ba, bb), osc, mi))
        if not meas:
            print("\n!! no usable measurements."); return 1

        # THERMAL DETREND: order was random, so any linear trend of mean|I| / ripple vs measurement
        # INDEX is heating (a time effect), not settling. Remove it (keep the mean) before aggregating.
        def _detrend(vals, idxs):
            n = len(vals); mx = sum(idxs) / n; my = sum(vals) / n
            den = sum((x - mx) ** 2 for x in idxs)
            b = (sum((idxs[i] - mx) * (vals[i] - my) for i in range(n)) / den) if den else 0.0
            return [vals[i] - b * (idxs[i] - mx) for i in range(n)], b
        idxs = [m[3] for m in meas]
        mis_d, b_mi = _detrend([m[2] for m in meas], idxs)
        osc_d, _b = _detrend([m[1] for m in meas], idxs)
        drift_mi = b_mi * (max(idxs) - min(idxs))

        def _sd(a):
            if len(a) < 2:
                return 0.0
            m = sum(a) / len(a); return (sum((x - m) ** 2 for x in a) / len(a)) ** 0.5
        acc = {s: {'mi': [], 'osc': []} for s in settles}
        for i, (st, _o, _m, _t) in enumerate(meas):
            acc[st]['mi'].append(mis_d[i]); acc[st]['osc'].append(osc_d[i])
        rows = [{'st': s, 'mi': sum(acc[s]['mi']) / len(acc[s]['mi']),
                 'osc': sum(acc[s]['osc']) / len(acc[s]['osc']), 'mi_sd': _sd(acc[s]['mi'])}
                for s in settles if acc[s]['mi']]

        print("\n" + "=" * 68)
        print("thermal drift removed: mean|I| drifted {:+.0f} mA over the run (detrended out).".format(
            drift_mi))
        # THE RING FLOOR: mean|I| is INFLATED below the floor (sampling in the switching ring) and
        # collapses to a PLATEAU once the ring clears. EARLIEST settle at the plateau = floor = MAX duty.
        tol = float(args.floor_tol)
        mi_plateau = min(r['mi'] for r in rows)
        print("mean|I| plateau (ring cleared) = {:.0f} mA\n".format(mi_plateau))
        print("{:>8} {:>9} {:>6} {:>9}   {}".format("settle", "mean|I|", "+-sd", "ripple", "inflation"))
        for r in rows:
            infl = 100.0 * (r['mi'] - mi_plateau) / mi_plateau if mi_plateau > 0 else 0.0
            print("{:>8} {:>9.0f} {:>6.0f} {:>9.0f}   {:>+5.0f}%  {}".format(
                r['st'], r['mi'], r['mi_sd'], r['osc'], infl, "#" * min(40, int(infl))))
        floor = min((r for r in rows if r['mi'] <= mi_plateau * (1 + tol)), key=lambda r: r['st'])
        pick = floor['st']
        print("-" * 68)
        print(">>> RING FLOOR = {} ns  (earliest settle within {:.0f}% of plateau = ring cleared). "
              "This is the LOWEST MaxSettlingTime for MAX DUTY.".format(pick, tol * 100.0))
        print(">>> The +-sd column is per-settle repeatability across the {} passes (small = solid). "
              "Anything below the floor samples in the ring (inflated current + more ripple).".format(
                  args.passes))

        if args.set or args.save:
            node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            node.sdo[IDX_SETTLE[0]][IDX_SETTLE[1]].raw = int(pick)
            orig_settle = None
            msg = "APPLIED MaxSettlingTime = {} ns (live)".format(pick)
            if args.save:
                try:
                    node.sdo['Save']['Single'].raw = ((IDX_SETTLE[0] << 8) | IDX_SETTLE[1])
                    msg += " + SAVED to NV"
                except Exception as e:
                    msg += " (NV save FAILED: {})".format(e)
            print("\n" + msg)
            print(">>> NEXT: run the full current-sense cal (bias/gain/slope/offset) AT this settling.")
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['Motor']['ud'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if orig_bias is not None:                    # always restore the bias we overwrote
                    node.sdo[0x3008][3].raw = orig_bias[0]; node.sdo[0x3009][3].raw = orig_bias[1]
                if orig_settle is not None:
                    node.sdo[IDX_SETTLE[0]][IDX_SETTLE[1]].raw = orig_settle
                print("\nRestored: bias {}, settling {} ns, IDLE.".format(
                    orig_bias, orig_settle if orig_settle is not None else "(kept new pick)"))
        except Exception as e:
            print("(cleanup warning: {} -- verify bias/settling restored)".format(e))
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
