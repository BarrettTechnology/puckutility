#!/usr/bin/env python3
"""Open-loop TORQUE probe for the hold twitch: is the ~57-86 Hz ring the outer loops, and what
is the gearbox mode's damping?

Two questions, one tool. Both run in Cyclic-Sync-Torque (mode 10), which has NO velocity and NO
position loop -- the drive applies the commanded torque and nothing else.

  --dc N        Apply a small constant torque and watch. The current loop is fully active at a
                real operating point but no outer loop is closed, so a 57-86 Hz ring here would
                mean the current loop (or the mechanics) self-excites. A clean trace pins the
                twitch on the velocity/position loops.

  --sweep       Stepped-sine torque injection, zero mean. Measures the torque->position response
                (normalised to acceleration) to find the gearbox torsional mode and, from the
                width of its peak, the DAMPING. That number decides whether active damping is
                tractable -- plant_id.py cannot make it, the position loop rolls off first.

                STATUS 2026-08-28: harness works and is safe, but the FIRST RESULT IS NOT
                TRUSTWORTHY and no damping figure should be quoted from it yet. Over 50-120 Hz
                the acceleration response declined monotonically 2.27e5 -> 1.0e5 with NO peak,
                so the swept range never bracketed a resonance and the printed Q came from the
                range edge. Worse, |accel|/torque should be FLAT (= 1/J) for a free inertia --
                the decline says something is attenuating that is not in the model. Two known
                confounds before this can be believed:
                  1. The input is COMMANDED torque, not applied. Small amplitudes are quantised
                     hard by int(round()) -- amp 4 gives a 5-level staircase.
                  2. CurrentFeedback cannot substitute as the input: the q-axis display filter
                     (pwm.c, from Rt/Lq) is only fc_q = 33.8 Hz on this puck, attenuating 0.42x
                     at 50 Hz and 0.08x at 120 Hz.
                Fix before re-running: widen below 50 Hz (needs a locked rotor or a raised
                velocity guard -- 3 per-mille at 30 Hz already trips 8000 cts/s), and get an
                honest applied-torque reference.

SAFETY -- read before raising any amplitude.

  motion_trq_control() in the firmware enforces ONLY torque saturation and i2t. The
  speed-dependent current limit is commented out (see app/motion.c), so in CST there is NO
  SPEED LIMIT: a sustained DC torque accelerates until voltage saturation. Mitigations here:

    * The sweep is ZERO-MEAN. A symmetric sine applies no net torque, so it cannot produce
      sustained acceleration -- this is why the sweep is the safe mode and --dc is the careful one.
    * Amplitude is capped at TRQ_AMP_CAP (per-mille), which keeps peak iq under the |iq| guard.
    * Host-side aborts on velocity, position excursion, |iq| and |id|; all cut torque to 0 first.
    * The firmware zeroes torque on SYNC loss (motion_eval_cst), so killing this script is safe.
    * finally: TargetTorque = 0, then IDLE, on every exit path including Ctrl-C.
"""
import argparse
import os
import struct
import sys
import time
from timeit import default_timer as timer

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import can_backend                                    # noqa: F401  (installs the CAN fix)
import canopen
from canopen_runner import MODE_IDLE, MODE_CYCLIC_SYNC_TRQ
import flywheel_test as fw                            # CW_* constants, _faulted, EDS path

# --- safety envelope -------------------------------------------------------------------------
TRQ_AMP_CAP   = 100      # per-mille of rated torque. 100 -> ~4.0 A peak = 14% of i_peak on a
                         # P4-32, comfortably under the 20% |iq| abort. Do not raise blindly:
                         # the mapping is iq_mA = demand * 1.414 * max_torque / kt.
VEL_ABORT     = 8000     # cts/s. MUCH tighter than the twitch test's 30000: there is no velocity
                         # loop here to arrest anything, so drift must be caught early.
POS_ABORT     = 8192     # cts from start (~2 motor rev). Catches slow creep the velocity guard
                         # would not -- a small net torque offset integrates quietly.
IQ_ABORT_FRAC = 0.20
ID_ABORT_FRAC = 0.10
# Soft-clamp ("electronic clamp") gains. A ~1 Hz position trim added to the torque command
# holds the average position so the free rotor cannot drift or run away, which is what forced
# the tiny amplitudes and the >=50 Hz floor on the first attempt. Deliberately feeble: at a
# 9-count injection swing this contributes ~0.05 per-mille against an injection of 4-15, so it
# is transparent in the 25-130 Hz band under test. Do NOT raise these into a real position
# loop -- that reintroduces the very controller whose absence is the point of CST.
TRIM_KP       = 0.004    # per-mille per count of error
TRIM_KI       = 2.0e-5   # per-mille per count per sample
TRIM_CAP      = 40       # per-mille ceiling on the trim contribution
FREQ_REF      = 100.0    # Hz at which --amp is the literal amplitude; see the constant-velocity
                         # note in the sweep loop for why amplitude tracks frequency.


def _drop(node):
    """Cut torque, then drop the drive. Torque first: it is the thing doing work."""
    for fn in (lambda: node.rpdo[1].__setitem__('TargetTorque', 0),
               lambda: node.sdo.download(0x6071, 0, struct.pack('<h', 0)),
               lambda: node.sdo.__setitem__('SetModeOfOperation', MODE_IDLE),
               lambda: node.sdo.__setitem__('ControlWord', 0x06)):
        try:
            fn()
        except Exception:
            pass


def _dump(csv_dir, tag, st):
    """Write the burst so it can be re-examined offline -- for self-excitation at 57-86 Hz that
    was not at the drive frequency, or to tell a real response from a settling transient."""
    if not st['T']:
        return None
    try:
        os.makedirs(csv_dir, exist_ok=True)
        path = os.path.join(csv_dir, "torque_{}_{}.csv".format(
            tag, time.strftime("%Y%m%d-%H%M%S")))
        with open(path, 'w') as fh:
            fh.write("t_s,pos_cts,vel_cts_s,iq_mA,trq_cmd\n")
            for row in zip(st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ']):
                fh.write("{:.6f},{},{},{:.1f},{:.3f}\n".format(*row))
        return path
    except Exception:
        return None


def _lockin(t, y, f):
    """Single-bin DFT at f: returns (amplitude, phase_rad). A lock-in rather than a full FFT so
    the estimate is unaffected by the drive frequency falling between FFT bins."""
    w = np.exp(-2j * np.pi * f * np.asarray(t))
    c = np.dot(np.asarray(y, float) - np.mean(y), w) / len(y)
    return 2.0 * abs(c), np.angle(c)


def run(can_device='can0', node_id=127, rate_hz=1000.0, amp=25, freqs=None, dc=None,
        secs=3.0, cycles=40, settle_frac=0.4, csv_dir='twitchtests', vel_abort=None,
        amp_exp=2.0, verbose=True):
    net = canopen.Network()
    net.connect(bustype='socketcan', channel=can_device)
    node = net.add_node(node_id, fw.EDS)
    st = {'cap': False, 'trq': 0.0, 'phase': 0.0, 'dphase': 0.0, 'gain': 0.0,
          'T': [], 'POS': [], 'VEL': [], 'IQ': [], 'TRQ': [], 'start': 0.0,
          'abort': '', 'p0': 0, 'trim_on': False, 'ierr': 0.0, 'trim': 0.0}
    results = []
    global VEL_ABORT
    if vel_abort:
        VEL_ABORT = int(vel_abort)
    try:
        i_peak = node.sdo['Calibration']['i_peak'].raw
        max_trq = struct.unpack('<I', node.sdo.upload(0x6076, 0))[0]
        kt = struct.unpack('<H', node.sdo.upload(0x3011, 4))[0]
        amp = int(min(abs(amp), TRQ_AMP_CAP))
        iq_pk = amp * 1.414 * max_trq / kt
        if verbose:
            print("# torque_probe node {}: CST (mode 10), SYNC {:.0f} Hz".format(node_id, rate_hz))
            print("# amplitude {} per-mille -> ~{:.0f} mA peak ({:.1f}% of i_peak {})".format(
                amp, iq_pk, 100 * iq_pk / i_peak, i_peak))
            print("# guards: |vel|<{} cts/s  |pos-p0|<{} cts  |iq|<{:.0%}  |id|<{:.0%} of i_peak".format(
                VEL_ABORT, POS_ABORT, IQ_ABORT_FRAC, ID_ABORT_FRAC))

        node.tpdo.read(); node.rpdo.read()
        if node.rpdo[1].cob_id is None:
            node.rpdo[1].cob_id = 0x200 + node_id

        def _cb(_msg):
            # SYNC-driven: advance the sine and publish the new torque, then latch a sample.
            try:
                st['phase'] += st['dphase']
                drive = st['gain'] * (st['trq'] if st['dphase'] == 0.0
                                      else st['trq'] * np.sin(st['phase']))
                # Soft clamp: slow position trim so the free rotor holds station. Runs on the
                # RAW position error, which is dominated by drift -- the injected oscillation is
                # only a few counts and the loop is far too slow to answer it.
                trim = 0.0
                if st['trim_on']:
                    err = st['p0'] - (st['POS'][-1] if st['POS'] else st['p0'])
                    st['ierr'] += err
                    trim = TRIM_KP * err + TRIM_KI * st['ierr']
                    trim = max(-TRIM_CAP, min(TRIM_CAP, trim))
                    st['trim'] = trim
                trq = drive + trim
                node.rpdo[1]['TargetTorque'].raw = int(round(trq))
                # Guard HERE, in the SYNC callback, not in the burst loop. CST has no speed
                # limit, so a DC offset accelerates hard: a 200 Hz polling loop overshot an
                # 8000 cts/s ceiling by 2.4x (measured). At 750 Hz the blind window is 1.3 ms.
                # Zero the torque inline -- the burst loop then tears down the drive.
                if not st['abort']:
                    v = st['VEL'][-1] if st['VEL'] else 0
                    p = st['POS'][-1] if st['POS'] else st['p0']
                    if abs(v) > VEL_ABORT:
                        st['abort'] = 'vel {:.0f} cts/s > {}'.format(v, VEL_ABORT)
                    elif abs(p - st['p0']) > POS_ABORT:
                        st['abort'] = 'excursion {:.0f} cts > {}'.format(p - st['p0'], POS_ABORT)
                    if st['abort']:
                        st['gain'] = 0.0
                        node.rpdo[1]['TargetTorque'].raw = 0
                if st['cap']:
                    st['T'].append(timer() - st['start'])
                    st['POS'].append(node.tpdo[1]['PositionFeedback'].raw)
                    st['VEL'].append(node.tpdo[2]['VelocityFeedback'].raw)
                    st['IQ'].append(node.tpdo[2]['CurrentFeedback'].raw / 1000.0 * i_peak)
                    st['TRQ'].append(drive)
            except Exception:
                pass

        node.tpdo[2].add_callback(_cb)
        node.sdo['HeartbeatPeriod'].raw = 0
        node.sdo['ControlWord'].raw = fw.CW_FAULTRESET
        node.sdo['ControlWord'].raw = fw.CW_SHUTDOWN
        node.sdo['ControlWord'].raw = fw.CW_ENABLE
        # InterpolationPeriod is whole milliseconds (scale -3). If 1/rate is not an integer
        # ms the firmware interpolates CST torque against the wrong period -- at 750 Hz it was
        # told 1 ms while SYNC arrived every 1.333 ms, over-extrapolating every cycle.
        per_ms = 1000.0 / rate_hz
        if abs(per_ms - round(per_ms)) > 1e-6:
            print("# WARNING: {:.0f} Hz = {:.3f} ms is not a whole number of milliseconds; "
                  "CST interpolation will be wrong. Use 1000 or 500 Hz.".format(rate_hz, per_ms))
        try:
            node.sdo['Cyclic']['InterpolationPeriod'].raw = int(round(per_ms))
            node.sdo['Cyclic']['InterpolationScale'].raw = -3
        except Exception:
            pass
        node.rpdo[1]['ControlWord'].raw = fw.CW_ENABLE
        node.rpdo[1]['SetModeOfOperation'].raw = MODE_CYCLIC_SYNC_TRQ
        node.rpdo[1]['TargetTorque'].raw = 0
        st['p0'] = node.sdo['PositionFeedback'].raw
        if verbose:
            print("# soft clamp: ~1 Hz position trim (Kp {:.3g}, Ki {:.3g}, cap {} per-mille) "
                  "holds station without a mechanical clamp".format(TRIM_KP, TRIM_KI, TRIM_CAP))
        node.rpdo[1].start(1.0 / rate_hz)
        net.sync.start(1.0 / rate_hz)
        time.sleep(0.4)

        def _burst(f, dur, label, amp_f=None):
            """One excitation burst. f=0 -> DC. Returns False on a safety abort."""
            st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
            st['phase'], st['dphase'] = 0.0, (2 * np.pi * f / rate_hz if f else 0.0)
            st['trq'], st['gain'] = float(amp_f if amp_f else amp), 0.0
            st['abort'] = ''
            st['ierr'] = 0.0                      # fresh integral each burst
            st['start'] = timer(); st['cap'] = True
            t0 = time.time(); ramp = min(0.15, dur * 0.2)
            k = 0
            while True:
                el = time.time() - t0
                if el >= dur:
                    break
                # raised-cosine ramp in/out: a torque STEP would ring the very mode we are
                # trying to measure, contaminating the first cycles of every burst.
                if el < ramp:
                    st['gain'] = 0.5 * (1 - np.cos(np.pi * el / ramp))
                elif el > dur - ramp:
                    st['gain'] = 0.5 * (1 - np.cos(np.pi * (dur - el) / ramp))
                else:
                    st['gain'] = 1.0
                if st['abort']:                       # set inline by the SYNC callback
                    break
                if fw._faulted(node):
                    st['abort'] = 'drive fault'; break
                if st['IQ'] and abs(st['IQ'][-1]) > IQ_ABORT_FRAC * i_peak:
                    st['abort'] = 'iq {:+.0f} mA'.format(st['IQ'][-1]); break
                k += 1
                if k % 3 == 0:
                    try:
                        idr = int.from_bytes(node.sdo.upload(0x3010, 6), 'little', signed=True)
                        if abs(idr / 1000.0 * i_peak) > ID_ABORT_FRAC * i_peak:
                            st['abort'] = 'id {:+.0f} mA'.format(idr / 1000.0 * i_peak); break
                    except Exception:
                        pass
                time.sleep(0.002)
            st['gain'] = 0.0
            st['cap'] = False
            node.rpdo[1]['TargetTorque'].raw = 0
            if st['abort']:
                _drop(node)
                print("  !! SAFETY ABORT ({}) during {} -> torque cut, drive dropped.".format(
                    st['abort'], label))
                return False
            time.sleep(0.25)                                   # let it settle between bursts
            return True

        # ---------------- DC probe: does the ring exist with NO outer loop? ----------------
        if dc is not None:
            st['trq'] = float(min(abs(dc), TRQ_AMP_CAP)) * (1 if dc >= 0 else -1)
            if verbose:
                print("\n# DC probe: constant torque {} per-mille for {:.1f}s. No velocity or "
                      "position loop is closed.".format(int(st['trq']), secs))
            amp = int(abs(st['trq'])) or 1
            st['trim_on'] = False          # a trim would fight the very torque under test
            if _burst(0.0, secs, 'DC probe'):
                _dump(csv_dir, "dc{}".format(int(st['trq'])), st)
                v = np.asarray(st['VEL'], float); t = np.asarray(st['T'], float)
                dt = np.median(np.diff(t)) if t.size > 2 else 1.0 / rate_hz
                V = np.abs(np.fft.rfft((v - v.mean()) * np.hanning(v.size)))
                fr = np.fft.rfftfreq(v.size, dt)
                m = fr > 5
                pk = fr[m][V[m].argmax()]
                band = (fr >= 50) & (fr <= 95)
                frac = V[band].sum() / max(V[m].sum(), 1e-9)
                print("  moved {:>7.0f} cts   vel_rms {:>6.0f} cts/s   dominant {:>5.1f} Hz"
                      "   50-95 Hz energy {:.0%}".format(
                          st['POS'][-1] - st['p0'] if st['POS'] else 0, v.std(), pk, frac))
                print("  verdict: {}".format(
                    "RING PRESENT in open-loop torque -> NOT purely the outer loops"
                    if frac > 0.35 and v.std() > 300 else
                    "no gearbox ring in open-loop torque -> the twitch needs the velocity loop"))
                results.append({'mode': 'dc', 'trq': st['trq'], 'vel_rms': float(v.std()),
                                'dom_freq': float(pk), 'band_frac': float(frac)})

        # ---------------- stepped-sine sweep: the resonance Bode ----------------
        if freqs:
            fref = max(freqs)
            if verbose:
                print("\n# amplitude scales as (f/{:.0f})^{:.1f}; --amp is the value at {:.0f} Hz"
                      .format(fref, amp_exp, fref))
                print("\n#   f Hz   amp   pos amp   |acc|/trq   phase deg   vel amp")
            for f in freqs:
                dur = max(cycles / f, 1.0)
                # CONSTANT-VELOCITY excitation: for a free inertia the velocity amplitude goes
                # as T/(J*omega), so a fixed torque produces a huge swing at the low end -- 10
                # per-mille at 30 Hz measured 9676 cts/s and tripped the guard. Scaling the
                # amplitude with f holds the velocity roughly flat across the sweep, which keeps
                # every point inside the envelope AND improves SNR at the top end. The measured
                # quantity is the RATIO |vel|/torque, so this does not distort the transfer
                # function as long as the amplitude actually used is the one divided out.
                # CONSTANT-POSITION excitation: position amplitude goes as T/(J*omega^2), so
                # amplitude must scale as f^2 to keep it flat. The earlier f^1 (constant-
                # velocity) rule starved the top end -- 130 Hz returned 0.4 counts, at the
                # encoder's 1-count resolution, which is why those points were noise. --amp is
                # the amplitude at the TOP of the sweep, where the plant needs the most drive.
                amp_f = int(max(1, min(TRQ_AMP_CAP, round(amp * (f / fref) ** amp_exp))))
                st['trim_on'] = True
                if not _burst(f, dur, "{:.0f} Hz".format(f), amp_f=amp_f):
                    _dump(csv_dir, "f{:.0f}_ABORT".format(f), st)
                    break
                trace = _dump(csv_dir, "f{:.0f}".format(f), st)
                t = np.asarray(st['T'], float)
                n0 = int(len(t) * settle_frac)                 # drop ramp-in + transient
                t, v = t[n0:], np.asarray(st['VEL'], float)[n0:]
                iq = np.asarray(st['IQ'], float)[n0:]
                if t.size < 20:
                    continue
                # Lock in on POSITION, not velocity. VelocityFeedback is a 1 kHz position
                # difference, so it is quantised at ~1000 cts/s -- at the amplitudes the safety
                # envelope allows, a velocity lock-in measures quantisation noise (measured:
                # response alternating 492/62/256/41 with no smooth structure). Position is the
                # directly-measured quantity at 1-count resolution, and averaging over `cycles`
                # buys another sqrt(N).
                pos = np.asarray(st['POS'], float)[n0:]
                kk = np.arange(pos.size)
                pos = pos - np.poly1d(np.polyfit(kk, pos, 1))(kk)   # strip drift/creep
                pa, pp = _lockin(t, pos, f)
                va, vp = _lockin(t, v, f)
                ia, _ = _lockin(t, iq, f)
                # Normalise to ACCELERATION: for a free inertia |accel|/T is flat (= 1/J), so a
                # resonance shows as structure instead of hiding under a 1/f^2 compliance slope.
                acc = (2 * np.pi * f) ** 2 * pa / max(amp_f, 1)
                results.append({'mode': 'sweep', 'f': f, 'amp': amp_f, 'pos_amp': pa,
                                'vel_amp': va, 'resp': acc, 'phase_deg': np.degrees(pp),
                                'iq_amp': ia, 'trace': trace})
                if verbose:
                    print("  {:>6.1f}  {:>4d}   {:>8.1f}   {:>9.3g}   {:>9.1f}   {:>7.0f}".format(
                        f, amp_f, pa, acc, np.degrees(pp), va))
        return results, st
    finally:
        try:
            _drop(node)
            net.sync.stop(); node.rpdo[1].stop()
        except Exception:
            pass
        try:
            node.tpdo[2].callbacks.remove(_cb)
        except Exception:
            pass
        try:
            node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            node.sdo['ControlWord'].raw = 0x06
            print("# restored: TargetTorque=0, IDLE")
        except Exception:
            pass
        net.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--can', default='can0')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--rate', type=float, default=1000.0,
                    help='SYNC/PDO rate Hz (default 1000). MUST divide evenly into whole '
                         'milliseconds: InterpolationPeriod is an integer ms field, so 750 Hz '
                         '(1.333 ms) is written as 1 ms and the firmware then interpolates CST '
                         'torque against the wrong period. Use 1000 (1 ms) or 500 (2 ms).')
    ap.add_argument('--amp', type=int, default=25,
                    help='sine torque amplitude at {:.0f} Hz, per-mille of rated (default 25 '
                         '~= 1.0 A peak). Scaled proportional to frequency to hold the velocity '
                         'swing flat; capped at {}.'.format(FREQ_REF, TRQ_AMP_CAP))
    ap.add_argument('--sweep', default=None,
                    help='stepped-sine sweep "start:stop:step" Hz, e.g. "20:160:10"')
    ap.add_argument('--dc', type=int, default=None,
                    help='constant-torque probe at this per-mille instead of a sweep. NO SPEED '
                         'LIMIT exists in CST -- keep this small and watch the abort guards.')
    ap.add_argument('--secs', type=float, default=3.0, help='DC probe duration s (default 3)')
    ap.add_argument('--cycles', type=int, default=40,
                    help='drive cycles per sweep point (default 40)')
    ap.add_argument('--csv-dir', default='twitchtests')
    ap.add_argument('--amp-exp', type=float, default=2.0,
                    help='amplitude scales as (f/f_top)^EXP (default 2.0 = constant position '
                         'amplitude). Use 1.0 for constant velocity, 0 for constant torque.')
    ap.add_argument('--vel-abort', type=int, default=None,
                    help='velocity ceiling cts/s (default {}). The sweep is zero-mean and soft-'
                         'clamped so it cannot run away; raising this is what lets the sweep '
                         'reach below ~45 Hz, where a free rotor swings hardest. For scale, the '
                         'twitch harness tolerates 30000 and a normal twitch peaks ~5000.'.format(VEL_ABORT))
    args = ap.parse_args()

    freqs = None
    if args.sweep:
        a, b, c = (float(x) for x in args.sweep.split(':'))
        freqs = list(np.arange(a, b + c / 2, c))
    if not freqs and args.dc is None:
        print("nothing to do: pass --sweep or --dc"); return 2

    res, st = run(args.can, args.node, rate_hz=args.rate, amp=args.amp, freqs=freqs,
                  dc=args.dc, secs=args.secs, cycles=args.cycles, csv_dir=args.csv_dir,
                  vel_abort=args.vel_abort, amp_exp=args.amp_exp)

    sw = [r for r in res if r.get('mode') == 'sweep']
    if len(sw) >= 3:
        f = np.array([r['f'] for r in sw]); g = np.array([r['resp'] for r in sw])
        ipk = int(g.argmax()); fpk, gpk = f[ipk], g[ipk]
        half = gpk / np.sqrt(2.0)
        lo = f[0]
        for i in range(ipk, 0, -1):
            if g[i] < half:
                lo = f[i]; break
        hi = f[-1]
        for i in range(ipk, len(f)):
            if g[i] < half:
                hi = f[i]; break
        print("\n==== RESONANCE ====")
        print("peak |accel|/torque at {:.1f} Hz  (gain {:.3g})".format(fpk, gpk))
        if ipk == 0 or ipk == len(f) - 1:
            print("peak sits on the EDGE of the swept range -- the resonance is outside it. "
                  "No Q reported; widen --sweep past {:.0f} Hz.".format(fpk))
        elif hi > lo and lo > f[0] and hi < f[-1]:
            Q = fpk / (hi - lo)
            print("-3dB width {:.1f}-{:.1f} Hz -> Q ~ {:.1f}, damping ratio ~ {:.3f}".format(
                lo, hi, Q, 1.0 / (2 * Q)))
            print("interpretation: {}".format(
                "sharp, lightly damped -> active damping / notch has something to grab"
                if Q > 5 else
                "broad, well damped -> no single frequency to target; broadband gain is the lever"))
        else:
            print("-3dB points not bracketed by the swept range -- widen --sweep to bound Q.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
