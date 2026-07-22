#!/usr/bin/env python3
"""validate_puck.py -- one combined validation pass, repeated at a few MaxSettlingTime values.

Runs the three checks we care about, at each of several settling times, so you can see whether
MaxSettlingTime affects ANY of them -- and if it doesn't, drive it as LOW as possible to maximise the
usable duty cycle (settling time is dead time in the PWM window).

  BLOCK A  enc-comp A/B      -- low-speed hold+crawl osc + |I|, comp 0x3027:1 ON vs OFF   (from spin_check)
  BLOCK B  slope/offset A/B  -- same, toggling slope 0x3008:7/0x3009:7 + offset 0x3008:8/0x3009:8
  BLOCK C  top-speed vitals  -- spin to the velocity ceiling; PEAK/CONTINUOUS RPM, iq, %mod

At the end it tabulates the key metrics vs settling time and calls out whether settling changed
anything. Nothing is written to NV; original MaxSettlingTime + coeffs are restored on exit.

  scripts/validate_puck.py --node 127                  # settles 100,200,300,400 ns (default)
  scripts/validate_puck.py --node 1 --settles 50,300,600
  scripts/validate_puck.py --node 127 --skip-top       # low-speed blocks only (fast)

SAFE: current-limited by i_peak/i2t; stops on drive fault; restores everything on exit / Ctrl-C.
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
SW_FAULT = 0x08
FULL_MOD = 32000.0


def _imag(node, i_peak):
    _id = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
    _iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
    return (_id * _id + _iq * _iq) ** 0.5


def _osc(vs):
    if not vs:
        return 0.0
    m = sum(vs) / len(vs)
    return max(max(vs) - min(vs), (sum((x - m) ** 2 for x in vs) / len(vs)) ** 0.5)


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _i16(node, idx, sub):
    return int.from_bytes(node.sdo.upload(idx, sub), 'little', signed=True)


def _set_i16(node, idx, sub, val):
    node.sdo.download(idx, sub, int(val).to_bytes(2, 'little', signed=True))


def _enable_vel(node):
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo['ControlWord'].raw = cw
    node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL


def _sample(node, i_peak, vcmd, secs):
    node.sdo['TargetVelocity'].raw = int(vcmd)
    time.sleep(0.6)
    vs = []; imax = 0.0; t0 = time.time()
    while time.time() - t0 < secs:
        vs.append(node.sdo['VelocityFeedback'].raw)
        imax = max(imax, _imag(node, i_peak))
        if node.sdo['StatusWord'].raw & SW_FAULT:
            return None, imax
        time.sleep(0.01)
    return vs, imax


def _ab_block(node, i_peak, enc_res, set_on, set_off, speeds_rpm, secs=1.8):
    """Generic ON/OFF ripple A/B. set_on()/set_off() flip the registers. Returns list of dicts."""
    rpm_to_cts = enc_res / 60.0
    out = []
    for rpm in speeds_rpm:
        vcmd = rpm * rpm_to_cts
        set_on()
        vs, ion = _sample(node, i_peak, vcmd, secs)
        set_off()
        vs2, ioff = _sample(node, i_peak, vcmd, secs)
        if vs is None or vs2 is None:
            out.append({'rpm': rpm, 'fault': True}); break
        out.append({'rpm': rpm, 'osc_on': _osc(vs), 'i_on': ion, 'osc_off': _osc(vs2), 'i_off': ioff})
    return out


def _top_speed(node, i_peak, enc_res, max_vel, soak=8.0):
    """Spin to the velocity ceiling; return PEAK RPM, CONTINUOUS (tail) RPM/iq, and peak %mod.
    Bleeds off a hot i2t accumulator first (idle, cap 25 s) so a leftover fold can't truncate the peak
    and confound the settle-to-settle comparison."""
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    _t0 = time.time()
    while time.time() - _t0 < 25.0:
        try:
            if node.sdo[0x3025][1].raw <= 300:
                break
        except Exception:
            break
        time.sleep(0.5)
    _enable_vel(node)
    node.sdo['TargetVelocity'].raw = int(max_vel)
    T, RPM, IQ, MOD = [], [], [], []
    t0 = time.time()
    while time.time() - t0 < soak:
        RPM.append(abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res)
        IQ.append(node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak)
        ud = node.sdo['Motor']['ud'].raw; uq = node.sdo['Motor']['uq'].raw
        MOD.append((ud * ud + uq * uq) ** 0.5 / FULL_MOD * 100.0)
        if node.sdo['StatusWord'].raw & SW_FAULT:
            break
        time.sleep(0.02)
    node.sdo['TargetVelocity'].raw = 0
    node.sdo['SetModeOfOperation'].raw = MODE_IDLE
    n = max(1, len(RPM) // 4)
    return {'peak': max(RPM) if RPM else 0.0, 'cont': _mean(RPM[-n:]),
            'iq': _mean(IQ[-n:]), 'mod': _mean(MOD[-n:])}


def main():
    ap = argparse.ArgumentParser(description="Combined puck validation across MaxSettlingTime values.")
    ap.add_argument('--can', default='can0')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--settles', default='100,200,300,400',
                    help='MaxSettlingTime ns values to test (default 100,200,300,400)')
    ap.add_argument('--max-rpm', type=float, default=12.0, help='crawl-ladder top for the A/B blocks')
    ap.add_argument('--steps', type=int, default=2, help='crawl speeds (plus hold) in the A/B blocks')
    ap.add_argument('--skip-top', action='store_true', help='skip the high-speed block (faster)')
    args = ap.parse_args()

    settles = [int(s) for s in args.settles.split(',') if s.strip()]
    net = can_backend.make_network(args.can, bitrate=1_000_000)
    node = None
    orig = {}
    try:
        node = net.add_node(args.node, EDS)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        i_peak  = node.sdo['Calibration']['i_peak'].raw
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        try:
            max_vel = int(node.sdo['max_velocity'].raw)
        except Exception:
            max_vel = 0
        if max_vel <= 0:
            max_vel = int(round(15000.0 / 60.0 * enc_res))

        orig['settle'] = node.sdo['Amp']['MaxSettlingTime'].raw
        orig['comp'] = int(node.sdo[0x3027][1].raw)
        orig['sA'] = _i16(node, 0x3008, 7); orig['sB'] = _i16(node, 0x3009, 7)
        try:
            orig['oA'] = _i16(node, 0x3008, 8); orig['oB'] = _i16(node, 0x3009, 8); has_off = True
        except Exception:
            has_off = False

        def comp_on():  node.sdo[0x3027][1].raw = 1
        def comp_off(): node.sdo[0x3027][1].raw = 0
        def slope_on():
            _set_i16(node, 0x3008, 7, orig['sA']); _set_i16(node, 0x3009, 7, orig['sB'])
            if has_off: _set_i16(node, 0x3008, 8, orig['oA']); _set_i16(node, 0x3009, 8, orig['oB'])
        def slope_off():
            _set_i16(node, 0x3008, 7, 0); _set_i16(node, 0x3009, 7, 0)
            if has_off: _set_i16(node, 0x3008, 8, 0); _set_i16(node, 0x3009, 8, 0)

        speeds = [0.0] + [args.max_rpm * (i + 1) / args.steps for i in range(args.steps)]
        print("Node {}  enc_res={}  i_peak={} mA  ceiling {} cts/s (~{:.0f} RPM)".format(
            args.node, enc_res, i_peak, max_vel, max_vel * 60.0 / enc_res))
        print("Offset register: {};  testing MaxSettlingTime = {} ns\n".format(
            "present" if has_off else "ABSENT", settles))

        summary = []
        for st in settles:
            node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            node.sdo['Amp']['MaxSettlingTime'].raw = st
            rb = node.sdo['Amp']['MaxSettlingTime'].raw
            print("=" * 70)
            print("MaxSettlingTime = {} ns (readback {})".format(st, rb))
            print("=" * 70)

            _enable_vel(node)
            print("  BLOCK A  enc-comp A/B (0x3027:1):")
            slope_on()                                   # keep slope/offset at cal values for this block
            a = _ab_block(node, i_peak, enc_res, comp_on, comp_off, speeds)
            for r in a:
                if r.get('fault'): print("    {:>6.0f} RPM  FAULT".format(r['rpm'])); break
                print("    {:>5}  osc ON/OFF {:>6.0f}/{:<6.0f}  |I| ON/OFF {:>5.0f}/{:<5.0f}".format(
                    "hold" if r['rpm'] == 0 else "{:.0f}rpm".format(r['rpm']),
                    r['osc_on'], r['osc_off'], r['i_on'], r['i_off']))
            comp_on()

            print("  BLOCK B  slope+offset A/B (0x3008:7/8, 0x3009:7/8):")
            b = _ab_block(node, i_peak, enc_res, slope_on, slope_off, speeds)
            for r in b:
                if r.get('fault'): print("    {:>6.0f} RPM  FAULT".format(r['rpm'])); break
                print("    {:>5}  osc ON/OFF {:>6.0f}/{:<6.0f}  |I| ON/OFF {:>5.0f}/{:<5.0f}".format(
                    "hold" if r['rpm'] == 0 else "{:.0f}rpm".format(r['rpm']),
                    r['osc_on'], r['osc_off'], r['i_on'], r['i_off']))
            slope_on()

            top = None
            if not args.skip_top:
                print("  BLOCK C  top-speed:")
                top = _top_speed(node, i_peak, enc_res, max_vel)
                print("    PEAK {:.0f} RPM   CONT {:.0f} RPM   iq {:.0f} mA   %mod {:.0f}".format(
                    top['peak'], top['cont'], top['iq'], top['mod']))
                time.sleep(1.0)

            # per-settle scalars for the cross-settle comparison
            def _benefit(block):
                good = [r for r in block if not r.get('fault')]
                di = _mean([r['i_off'] - r['i_on'] for r in good]) if good else 0.0
                return di
            summary.append({'st': rb, 'comp_dI': _benefit(a), 'slope_dI': _benefit(b), 'top': top})

        # --- cross-settle comparison: did MaxSettlingTime change anything? ---
        print("\n" + "=" * 70)
        print("CROSS-SETTLE COMPARISON  (does MaxSettlingTime move any metric?)")
        print("{:>10} {:>12} {:>12} {:>10} {:>10} {:>7}".format(
            "settle", "comp dI", "slope dI", "peakRPM", "cont iq", "%mod"))
        for s in summary:
            t = s['top'] or {}
            print("{:>10} {:>12.0f} {:>12.0f} {:>10} {:>10} {:>7}".format(
                s['st'], s['comp_dI'], s['slope_dI'],
                "{:.0f}".format(t.get('peak', 0)) if s['top'] else "-",
                "{:.0f}".format(t.get('iq', 0)) if s['top'] else "-",
                "{:.0f}".format(t.get('mod', 0)) if s['top'] else "-"))
        if len(summary) >= 2 and summary[0]['top']:
            peaks = [s['top']['peak'] for s in summary]
            mods  = [s['top']['mod'] for s in summary]
            iqs   = [s['top']['iq'] for s in summary]
            peak_spread = max(peaks) - min(peaks)
            mod_spread  = max(mods) - min(mods)
            iq_spread   = max(iqs) - min(iqs)
            print("-" * 70)
            # PERFORMANCE = what the MOTOR does (RPM, %mod). iq is a MEASUREMENT -- the current SENSE
            # reads differently by sample timing (ring), which is expected and NOT a performance change.
            print("PERFORMANCE spread:  peak-RPM {:.0f} ({:.1f}%)   %mod {:.0f}".format(
                peak_spread, 100.0 * peak_spread / max(peaks), mod_spread))
            print("current-READING spread:  iq {:.0f} mA  -- NOT comparable across settling: the bias/"
                  "gain/slope/offset cal is settling-SPECIFIC, and this sweep did NOT re-cal, so both "
                  "readings apply a stale cal. Ignore this column for the settling decision.".format(
                      iq_spread))
            perf_flat = (peak_spread < 0.03 * max(peaks)) and (mod_spread < 3.0)
            if perf_flat:
                print(">>> MOTOR PERFORMANCE (RPM, %mod) is FLAT across settling -> settling does NOT "
                      "change what the motor does. (These are cal-INDEPENDENT, so this conclusion is "
                      "valid despite the stale-cal caveat above.)")
                print(">>> WORKFLOW: settling and the current-sense cal are COUPLED. Pick MaxSettlingTime "
                      "FIRST (low = max duty), then run the current-sense cal AT that settling; the cal "
                      "makes the reading accurate there. Don't hunt for a 'best' settling by iq.")
            else:
                print(">>> MOTOR performance itself moved with settling -> not free; pick the value with "
                      "the best peak-RPM / %mod, then cal the current sense at that value.")
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                if 'settle' in orig: node.sdo['Amp']['MaxSettlingTime'].raw = orig['settle']
                if 'comp' in orig: node.sdo[0x3027][1].raw = orig['comp']
                if 'sA' in orig:
                    _set_i16(node, 0x3008, 7, orig['sA']); _set_i16(node, 0x3009, 7, orig['sB'])
                if 'oA' in orig:
                    _set_i16(node, 0x3008, 8, orig['oA']); _set_i16(node, 0x3009, 8, orig['oB'])
                print("\nRestored: MaxSettlingTime, comp, slope/offset coeffs; IDLE.")
        except Exception as e:
            print("\n(cleanup warning: {})".format(e))
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
