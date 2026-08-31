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
# Crawl servo (cogmap only): a PD position loop, NOT the PI hold clamp. The hold clamp winds up
# during a stick and releases in a 17k cts/s lurch; a crawl needs to pace the rotor, not hold it.
# CRAWL_KP is stiff enough that a bounded lead error (see LEAD_CAP) just clears breakaway (~4
# per-mille); CRAWL_KD brakes each slip before it runs away; no integral => no wind-up.
# Slip velocity is set by physics: v_peak ~= (windup at break-free) * omega_mode. With the ~70 Hz
# torsional mode (omega ~440 rad/s), a soft servo that winds 27 cts before breaking free releases
# 27*440 ~= 12k cts/s. So the servo must be STIFF: break free at few counts of windup -> small
# energy release -> small slip. KP=0.35 breaks at ~11 cts (breakaway ~4 p-m) -> ~5k cts/s slip.
CRAWL_KP      = 0.15     # per-mille per count -> break-free windup ~27 cts
CRAWL_KD      = 0.0      # NO velocity feedback: lagged encvel feedback IS the twitch -- any KD
                         # rings the ~70 Hz mode instantly (measured: -21k cts/s in a fwd crawl).
                         # Pure-P can't self-excite; it just stick-slips, which the leash bounds.
LEAD_CAP      = 25       # cts the crawl setpoint may lead the rotor (hard cap on windup per slip)
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


def _ringdown_fit(t, y, fmin=25.0, fmax=170.0):
    """Try several analysis windows and keep the most convincing fit. A heavily damped ring is
    gone in ~40 ms, so judging it over a 0.8 s capture means 95% of the record is coast residual
    and noise, and the FFT locks onto that instead (measured: 55 Hz read back as 107 Hz). A
    lightly damped one needs the long window. Rather than guess, fit each and score."""
    best = None
    for win in (0.06, 0.10, 0.18, 0.35, 0.70):
        m = np.asarray(t, float) - t[0] <= win
        if m.sum() < 64:
            continue
        r = _ringdown_fit_one(np.asarray(t, float)[m], np.asarray(y, float)[m], fmin, fmax)
        if r is None:
            continue
        # score: trust r^2, but require enough envelope peaks to mean anything
        score = r[4] * min(r[3], 12)
        if best is None or score > best[0]:
            best = (score, r)
    return best[1] if best else None


def _ringdown_fit_one(t, y, fmin=25.0, fmax=170.0):
    """Damping from a free-decay trace. Returns (f_d, zeta, Q, n_peaks, r2) or None.

    Band-pass first: after an impulse the rotor also coasts, and that rigid-body ramp swamps
    the oscillation. Then fit an exponential to the envelope by log-linear regression through
    the successive peak magnitudes -- no scipy on this box, so no Hilbert transform.
    """
    t = np.asarray(t, float); y = np.asarray(y, float)
    n = y.size
    if n < 64:
        return None
    dt = float(np.median(np.diff(t)))
    # DETREND before the transform. After an impulse the rotor coasts, so position carries a
    # large ramp; an un-detrended ramp leaks broadband energy into every FFT bin including the
    # passband, and the "ring" that comes back out is leakage, not signal. (Measured: fitting
    # raw position gave 0 clean decays from 14, worse than velocity's 2 from 6, purely from
    # this.) A cubic also absorbs the friction-decelerated part of the coast.
    kk = np.arange(n, dtype=float)
    y = y - np.poly1d(np.polyfit(kk, y, 3))(kk)
    fr = np.fft.rfftfreq(n, dt)
    keep = (fr >= fmin) & (fr <= fmax)
    if not keep.any():
        return None
    # Window ONLY to estimate the frequency -- it suppresses leakage but imposes its own
    # rise-and-fall envelope, which would overwrite the exponential decay being measured.
    Yw = np.fft.rfft(y * np.hanning(n))
    fd = float(fr[keep][np.abs(Yw[keep]).argmax()])      # dominant decay frequency
    # Band-pass the UNWINDOWED signal for the envelope fit, so the decay is intact.
    Y = np.fft.rfft(y)
    yb = np.fft.irfft(np.where(keep, Y, 0), n)

    # successive |peaks| of the band-passed signal = the decay envelope
    pk_t, pk_v = [], []
    for i in range(1, n - 1):
        if abs(yb[i]) >= abs(yb[i - 1]) and abs(yb[i]) > abs(yb[i + 1]) and abs(yb[i]) > 0:
            pk_t.append(t[i]); pk_v.append(abs(yb[i]))
    if len(pk_t) < 4:
        return None
    pk_t = np.array(pk_t); pk_v = np.array(pk_v)
    # Truncate at the noise floor, CONTIGUOUSLY. Once the ring decays into noise the peaks stop
    # shrinking, and including them flattens the log-linear slope and under-reports damping
    # badly (validated: true zeta 0.05 came back as 0.004). Start at the largest peak and stop
    # at the first one that falls under the floor -- do not keep later peaks that poke back up.
    noise = float(np.median(np.abs(yb[int(n * 0.8):]))) if n > 32 else 0.0
    i0 = int(pk_v.argmax())
    floor = max(noise * 5.0, pk_v[i0] * 0.08)
    sel = [i0]
    for i in range(i0 + 1, len(pk_v)):
        if pk_v[i] < floor:
            break
        sel.append(i)
    pk_t, pk_v = pk_t[sel], pk_v[sel]
    if len(pk_t) < 4:
        return None
    A = np.vstack([pk_t, np.ones_like(pk_t)]).T
    sol, res, _, _ = np.linalg.lstsq(A, np.log(pk_v), rcond=None)
    sigma = -float(sol[0])                               # envelope ~ exp(-sigma t)
    lg = np.log(pk_v)
    ss = 1.0 - (np.sum((lg - A.dot(sol)) ** 2) / max(np.sum((lg - lg.mean()) ** 2), 1e-12))
    if sigma <= 0:
        return (fd, 0.0, float('inf'), len(pk_t), ss)    # not decaying -> self-sustaining
    wd = 2 * np.pi * fd
    zeta = sigma / np.sqrt(sigma ** 2 + wd ** 2)
    return (fd, float(zeta), float(1.0 / (2 * zeta)), len(pk_t), float(ss))


def _lockin(t, y, f):
    """Single-bin DFT at f: returns (amplitude, phase_rad). A lock-in rather than a full FFT so
    the estimate is unaffected by the drive frequency falling between FFT bins."""
    w = np.exp(-2j * np.pi * f * np.asarray(t))
    c = np.dot(np.asarray(y, float) - np.mean(y), w) / len(y)
    return 2.0 * abs(c), np.angle(c)


def run(can_device='can0', node_id=127, rate_hz=1000.0, amp=25, freqs=None, dc=None,
        secs=3.0, cycles=40, settle_frac=0.4, csv_dir='twitchtests', vel_abort=None,
        amp_exp=2.0, ring_n=0, ring_ms=7.0, ring_secs=0.6, brk_n=0,
        cogmap_revs=0, crawl_vel=200.0, dither_amps=None, dither_hz=250.0, dither_reps=4,
        verbose=True):
    net = canopen.Network()
    net.connect(bustype='socketcan', channel=can_device)
    node = net.add_node(node_id, fw.EDS)
    st = {'cap': False, 'trq': 0.0, 'phase': 0.0, 'dphase': 0.0, 'gain': 0.0,
          'T': [], 'POS': [], 'VEL': [], 'IQ': [], 'TRQ': [], 'start': 0.0,
          'abort': '', 'p0': 0, 'trim_on': False, 'ierr': 0.0, 'trim': 0.0, 'dc_bias': 0.0}
    results = []
    global VEL_ABORT
    if vel_abort:
        VEL_ABORT = int(vel_abort)
    try:
        i_peak = node.sdo['Calibration']['i_peak'].raw
        max_trq = struct.unpack('<I', node.sdo.upload(0x6076, 0))[0]
        kt = struct.unpack('<H', node.sdo.upload(0x3011, 4))[0]
        try:                                                # electrical cycle -> the cogging test
            _poles = int(node.sdo[0x3011][3].raw)
            enc_zero = int(node.sdo[0x3011][1].raw)
            enc_res = int(node.sdo['EncoderConfig']['Resolution'].raw)
            cts_elec = enc_res / max(1.0, _poles / 2.0)
        except Exception:
            cts_elec, enc_zero, enc_res = 585.14, 0, 4096
        try:                                                # gear ratio -> perceptibility at the output
            gear = float(node.sdo[0x6091][1].raw) / max(1.0, float(node.sdo[0x6091][2].raw))
        except Exception:
            gear = 60.84
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
                drive = st['gain'] * (st['dc_bias'] + (st['trq'] if st['dphase'] == 0.0
                                      else st['trq'] * np.sin(st['phase'])))
                # Soft clamp: slow position trim so the free rotor holds station. Runs on the
                # RAW position error, which is dominated by drift -- the injected oscillation is
                # only a few counts and the loop is far too slow to answer it.
                trim = 0.0
                if st['trim_on']:
                    err = st['p0'] - (st['POS'][-1] if st['POS'] else st['p0'])
                    if st.get('crawl'):
                        # PD crawl servo: stiff-but-damped, no integral (no wind-up -> no lurch).
                        v = st['VEL'][-1] if st['VEL'] else 0
                        trim = CRAWL_KP * err - CRAWL_KD * v
                    else:
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

        def _wait_still(span=3, need=0.4, timeout=8.0):
            """Block until the rotor has actually stopped. After a breakaway it coasts under
            zero torque, and starting the next ramp on a moving rotor trips the velocity guard
            immediately (observed: 8224 and 8670 cts/s).

            Stillness is judged on POSITION, not velocity. VelocityFeedback is a 1 kHz position
            difference quantised at ~1000 cts/s per count, so it reads several hundred cts/s
            even at a dead stop (measured +/-372 while parked in IDLE) -- any velocity
            threshold below that noise floor can never be satisfied."""
            t0 = time.time(); quiet = 0.0; last = time.time()
            while time.time() - t0 < timeout:
                w = st['POS'][-int(need * rate_hz):] if st['POS'] else []
                now = time.time()
                moving = (not w) or (max(w) - min(w)) > span
                quiet = 0.0 if moving else quiet + (now - last)
                last = now
                if quiet >= need and len(w) >= int(need * rate_hz * 0.5):
                    return True
                time.sleep(0.02)
            return False

        def _settle_and_anchor():
            """Wait for stillness and re-anchor the excursion reference. Must run BEFORE each
            rep, not just after: the rotor can still be coasting from a previous run entirely
            (observed: rep 0 aborted at 8670 cts/s on a freshly started sweep)."""
            st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
            st['start'] = timer()
            st['gain'] = 0.0                      # nothing driving while we wait
            st['cap'] = True
            ok = _wait_still()
            st['cap'] = False
            try:
                st['p0'] = node.sdo['PositionFeedback'].raw
            except Exception:
                pass
            return ok

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

        # ---------------- RINGDOWN: impulse, then watch the free decay ----------------
        # The cleanest way to get the mode's damping on a plant dominated by stiction. A single
        # large-amplitude transient does not care whether small-signal drive clears breakaway,
        # and no sustained excitation means nothing can run away. Output shaft free is fine:
        # motor and load ring against each other through the gearbox spring.
        if ring_n:
            if verbose:
                print("\n# ringdown: {} impulses of {} per-mille x {:.1f} ms, then zero torque"
                      .format(ring_n, amp, 1000.0 * ring_ms / 1000.0))
                print("#   rep   f_d Hz   zeta     Q    peaks    r2   note")
            st['trim_on'] = False               # a trim would inject energy into the decay
            for rep in range(ring_n):
                if not _settle_and_anchor():
                    print("  (rotor will not settle -- aborting ringdown)"); break
                st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
                st['abort'] = ''
                st['phase'], st['dphase'] = 0.0, 0.0
                st['trq'], st['gain'] = float(amp), 1.0
                st['start'] = timer(); st['cap'] = True
                t0 = time.time()
                # half-sine impulse: concentrates energy near 1/(2*width), and unlike a square
                # pulse it does not slam the current loop with a step.
                while time.time() - t0 < ring_ms / 1000.0:
                    frac = (time.time() - t0) / (ring_ms / 1000.0)
                    st['gain'] = float(np.sin(np.pi * min(frac, 1.0)))
                    if st['abort']:
                        break
                    time.sleep(0.0005)
                st['gain'] = 0.0                        # release: pure free decay from here
                node.rpdo[1]['TargetTorque'].raw = 0
                t1 = time.time()
                while time.time() - t1 < ring_secs and not st['abort']:
                    time.sleep(0.002)
                st['cap'] = False
                if st['abort']:
                    _dump(csv_dir, "ring{}_ABORT".format(rep), st)
                    _drop(node)
                    print("  !! SAFETY ABORT ({}) during ringdown -> drive dropped.".format(
                        st['abort']))
                    break
                tr = _dump(csv_dir, "ring{}".format(rep), st)
                t = np.asarray(st['T'], float)
                n0 = int(np.searchsorted(t, ring_ms / 1000.0 * 1.5))   # analyse AFTER release
                # Fit POSITION, not velocity. VelocityFeedback is quantised at ~1000 cts/s, so a
                # decaying 70 Hz ring falls under the floor after a cycle or two and the envelope
                # dies early -- that is why the first attempt got only 2 clean fits from 6. The
                # same ring is a few counts of position against 1-count resolution. The fit
                # band-passes anyway, so the rigid-body coast is removed for free.
                fit = _ringdown_fit(t[n0:], np.asarray(st['POS'], float)[n0:])
                if fit is None:
                    print("  {:>5}   no clean decay found (too few envelope peaks -- the mode "
                          "is either absent or dead in under ~2 cycles)".format(rep))
                    results.append({'mode': 'ring', 'rep': rep, 'fit': None, 'trace': tr})
                    continue
                fd, zeta, Q, npk, r2 = fit
                results.append({'mode': 'ring', 'rep': rep, 'f_d': fd, 'zeta': zeta, 'Q': Q,
                                'peaks': npk, 'r2': r2, 'trace': tr})
                if verbose:
                    print("  {:>5}   {:>6.1f}  {:>6.3f}  {:>5.1f}  {:>5d}  {:>5.2f}   {}".format(
                        rep, fd, zeta, Q, npk, r2,
                        "weak fit" if r2 < 0.7 else ""))
                time.sleep(0.4)

        # ---------------- BREAKAWAY: how much torque before it moves at all? ----------------
        # Quantifies the stiction the whole limit cycle winds up against, and says whether it
        # is rotor-position dependent. Ramps slowly and stops the instant the rotor moves, so
        # it never reaches a speed worth guarding against.
        if brk_n:
            if verbose:
                print("\n# breakaway: slow torque ramp until motion, {} positions".format(brk_n))
                print("#   rep  theta_e  breakaway per-mille    mA    moved cts   snap cts/s")
            st['trim_on'] = False
            vals = []
            for rep in range(brk_n):
                if not _settle_and_anchor():
                    print("  (rotor will not settle -- aborting breakaway sweep)"); break
                st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
                st['abort'] = ''
                st['phase'], st['dphase'] = 0.0, 0.0
                st['gain'], st['trq'] = 1.0, 0.0
                p_start = node.sdo['PositionFeedback'].raw
                theta_e = ((p_start - enc_zero) % cts_elec) / cts_elec * 360.0
                st['start'] = timer(); st['cap'] = True
                found = None
                for d in range(0, TRQ_AMP_CAP + 1):
                    st['trq'] = float(d)
                    time.sleep(0.03)
                    if st['abort']:
                        break
                    now = st['POS'][-1] if st['POS'] else p_start
                    if abs(now - p_start) > 12:          # >12 cts = unambiguously broken free
                        found = d
                        break
                st['gain'] = 0.0; st['trq'] = 0.0
                node.rpdo[1]['TargetTorque'].raw = 0
                st['cap'] = False
                moved = (st['POS'][-1] - p_start) if st['POS'] else 0
                # Peak velocity of the SNAP. Breakaway on a geared axis is not gentle: the
                # gearbox winds up elastically, then releases stored torsional energy as
                # kinetic in one lurch. That lurch is the same event the twitch root cause
                # describes, so its size is worth recording rather than merely surviving.
                snap = max((abs(v) for v in st['VEL']), default=0)
                _dump(csv_dir, "brk{}".format(rep), st)
                if st['abort']:
                    _drop(node); print("  !! SAFETY ABORT ({})".format(st['abort'])); break
                if found is None:
                    print("  {:>5}  {:>5.0f}   did not break away by {} per-mille".format(
                        rep, theta_e, TRQ_AMP_CAP))
                else:
                    vals.append(found)
                    print("  {:>5}  {:>5.0f}   {:>17}  {:>6.0f}  {:>10.0f}  {:>9.0f}".format(
                        rep, theta_e, found, found * 1.414 * max_trq / kt, moved, snap))
                results.append({'mode': 'brk', 'rep': rep, 'breakaway': found, 'moved': moved,
                                'snap_cts_s': snap, 'theta_e': theta_e, 'pos': p_start})
                # settling happens at the TOP of the next rep, via _settle_and_anchor()
            if len(vals) >= 2:
                print("  breakaway {}-{} per-mille (mean {:.1f}, spread {:.0%}) -> {}".format(
                    min(vals), max(vals), float(np.mean(vals)),
                    (max(vals) - min(vals)) / max(np.mean(vals), 1e-9),
                    "position-DEPENDENT stiction" if (max(vals) - min(vals)) > 0.5 * np.mean(vals)
                    else "stiction roughly uniform with position"))
            # --- COGGING vs MESH: is the position-dependent breakaway ELECTRICAL-periodic? ---
            bp = [(r['theta_e'], r['breakaway']) for r in results
                  if r.get('mode') == 'brk' and r.get('breakaway') is not None]
            if len(bp) >= 6:
                th = np.deg2rad(np.array([p[0] for p in bp], float))
                bk = np.array([p[1] for p in bp], float)
                mean_bk = float(bk.mean())
                print("\n# breakaway vs ELECTRICAL angle  (cogging vs mesh test, {} pts, mean {:.1f} p-m):"
                      .format(len(bp), mean_bk))
                worst = 0.0
                for k in (1, 2, 3):                          # cogging shows as low electrical harmonics
                    c = 2.0 / len(bk) * float(np.sum(bk * np.cos(k * th)))
                    s = 2.0 / len(bk) * float(np.sum(bk * np.sin(k * th)))
                    a = float(np.hypot(c, s)); frac = 100.0 * a / max(mean_bk, 1e-9)
                    worst = max(worst, frac)
                    print("#   {}x electrical: amp {:.2f} per-mille = {:>3.0f}% of mean".format(k, a, frac))
                if worst > 30.0:
                    print("#   => breakaway is STRONGLY electrical-periodic ({:.0f}%): COGGING is a major part "
                          "of the stiction -> COGGING COMP (0x3028) is the right lever, test it.".format(worst))
                else:
                    print("#   => NOT strongly electrical-periodic ({:.0f}%): stiction is bearing/gearbox-mesh, "
                          "NOT cogging -> cogging comp won't help; dither / friction-FF is the play.".format(worst))
                print("#   (caveat: rotor rests at cogging detents post-snap, which biases theta_e sampling; "
                      "a controlled electrical-cycle sweep would confirm a positive.)")

        # ---------------- COGGING/FRICTION MAP: slow bidirectional crawl ----------------
        # The breakaway sweep can't sample the electrical cycle cleanly -- every break-free
        # releases stored gearbox windup as a violent, guard-tripping snap (measured 11k-25k
        # cts/s from 40-80 mA). Instead, crawl the rotor slowly under continuous position
        # control (the soft clamp chases a slowly-advancing setpoint -> no free release -> no
        # snap) through several electrical revs in BOTH directions, and read the torque the
        # crawl needs (MEASURED iq) at each electrical angle. Decompose:
        #   iq_fwd(theta_e) = cog(theta_e) + friction ;  iq_rev(theta_e) = cog(theta_e) - friction
        #   => (fwd+rev)/2 = cogging (position-conservative) ;  (fwd-rev)/2 = friction (dissipative)
        # If the conservative part is electrically periodic, cogging is real and 0x3028 is the
        # lever; if it washes out under theta_e-binning, the stiction is friction/mesh -> dither.
        # No explicit iq/id guard needed here: the crawl torque is the soft clamp, capped at
        # TRIM_CAP (40 per-mille ~ 11% of i_peak), already under the |iq| envelope.
        if cogmap_revs:
            span = cogmap_revs * cts_elec
            dwell = span / max(crawl_vel, 1.0)
            if verbose:
                print("\n# COGGING/FRICTION MAP: bidirectional crawl, {} elec revs ({:.0f} cts) "
                      "at {:.0f} cts/s (~{:.1f}s/dir)".format(cogmap_revs, span, crawl_vel, dwell))
                print("#   torque=soft clamp (cap {} p-m); iq(theta_e): fwd+rev=cogging, fwd-rev=friction"
                      .format(TRIM_CAP))
            st['dphase'] = 0.0; st['trq'] = 0.0; st['gain'] = 0.0
            dir_data = {}
            for direction in (+1, -1):
                lbl = 'fwd' if direction > 0 else 'rev'
                if not _settle_and_anchor():
                    print("  (rotor won't settle -- skipping {} crawl)".format(lbl)); continue
                p_base = st['p0']; st['ierr'] = 0.0
                st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
                st['abort'] = ''; st['trim_on'] = True; st['gain'] = 0.0; st['crawl'] = True
                st['start'] = timer(); st['cap'] = True
                n_steps = int(dwell * rate_hz); t0 = time.time()
                for i in range(n_steps):
                    target = p_base + direction * crawl_vel * (i / rate_hz)     # ideal ramp
                    cur = st['POS'][-1] if st['POS'] else p_base
                    # lead-leash: never let the setpoint get more than LEAD_CAP ahead of the rotor,
                    # so a stick can't wind up more than one small slip's worth of energy. The crawl
                    # then paces itself to how fast the joint can actually be coaxed along.
                    st['p0'] = (min(target, cur + LEAD_CAP) if direction > 0
                                else max(target, cur - LEAD_CAP))
                    if st['abort']:
                        break
                    slp = t0 + (i + 1) / rate_hz - time.time()
                    if slp > 0:
                        time.sleep(slp)
                st['cap'] = False; st['trim_on'] = False; st['gain'] = 0.0; st['crawl'] = False
                node.rpdo[1]['TargetTorque'].raw = 0
                _dump(csv_dir, "cog_{}".format(lbl), st)
                if st['abort']:
                    _drop(node)
                    print("  !! abort during {} crawl ({}) -- using partial data".format(lbl, st['abort']))
                pos = np.asarray(st['POS'], float); iq = np.asarray(st['IQ'], float)
                vel = np.asarray(st['VEL'], float)
                if pos.size < 50:
                    print("  {} crawl: too few samples".format(lbl)); continue
                n0 = min(int(0.15 * pos.size), int(0.4 * rate_hz))   # drop clamp catch-up transient
                pos, iq, vel = pos[n0:], iq[n0:], vel[n0:]
                moved = abs(pos[-1] - pos[0])
                the = ((pos - enc_zero) % cts_elec) / cts_elec * 360.0
                dir_data[direction] = (the, iq)
                if verbose:
                    print("  {} crawl: {} samp, moved {:.0f} cts ({:.1f} elec rev), mean|iq| {:.0f} mA, "
                          "vel {:.0f}+/-{:.0f}".format(lbl, pos.size, moved, moved / max(cts_elec, 1),
                          float(np.mean(np.abs(iq))), float(np.mean(vel)), float(np.std(vel))))
                results.append({'mode': 'cogmap', 'dir': direction, 'moved': float(moved)})

            if (+1 in dir_data) and (-1 in dir_data):
                NB = 24; edges = np.linspace(0, 360, NB + 1)
                ctr = 0.5 * (edges[:-1] + edges[1:])
                def _binmed(the, iq):
                    out = np.full(NB, np.nan)
                    idx = np.clip(np.digitize(the, edges) - 1, 0, NB - 1)
                    for b in range(NB):
                        s = iq[idx == b]
                        if s.size >= 3:
                            out[b] = np.median(s)         # median -> robust to stick-slip lurches
                    return out
                f_iq = _binmed(*dir_data[+1]); r_iq = _binmed(*dir_data[-1])
                ok = ~(np.isnan(f_iq) | np.isnan(r_iq))
                if ok.sum() >= max(8, NB // 2):
                    th = np.deg2rad(ctr[ok])
                    cons = 0.5 * (f_iq[ok] + r_iq[ok])          # cogging (conservative)
                    fric = 0.5 * (f_iq[ok] - r_iq[ok])          # friction (dissipative)
                    fric_lvl = float(np.median(np.abs(fric)))
                    cons_ac = cons - cons.mean()
                    print("\n# cogging/friction decomposition ({} of {} electrical bins populated):"
                          .format(int(ok.sum()), NB))
                    print("#   friction (fwd-rev)/2 : median |f| = {:.0f} mA  (stiction the loop winds against)"
                          .format(fric_lvl))
                    print("#   cogging  (fwd+rev)/2 : pk-pk {:.0f} mA, rms {:.0f} mA  vs electrical angle:"
                          .format(float(cons_ac.max() - cons_ac.min()), float(np.std(cons_ac))))
                    worst = 0.0
                    for k in (1, 2, 3, 6):                       # cogging = low electrical harmonics
                        c = 2.0 / th.size * float(np.sum(cons_ac * np.cos(k * th)))
                        s = 2.0 / th.size * float(np.sum(cons_ac * np.sin(k * th)))
                        a = float(np.hypot(c, s)); frac = 100.0 * a / max(fric_lvl, 1e-9)
                        worst = max(worst, frac)
                        print("#   {}x electrical: cogging amp {:.0f} mA = {:>3.0f}% of friction".format(k, a, frac))
                    if worst > 50.0:
                        print("#   => COGGING is a MAJOR part of the stiction ({:.0f}% of friction): "
                              "COGGING COMP (0x3028) is the right lever -- calibrate + test it.".format(worst))
                    elif worst > 20.0:
                        print("#   => cogging is a MINOR contributor ({:.0f}% of friction): worth a cogging-comp "
                              "test but dither/friction-FF is the primary lever.".format(worst))
                    else:
                        print("#   => cogging is NEGLIGIBLE ({:.0f}% of friction): the stiction is bearing/"
                              "gearbox friction -> cogging comp won't help, DITHER / friction-FF is the play."
                              .format(worst))
                    results.append({'mode': 'cogmap_result', 'friction_mA': fric_lvl,
                                    'cogging_rms_mA': float(np.std(cons_ac)), 'worst_elec_frac': worst})
                else:
                    print("  cogmap: too few populated electrical bins ({}) -- crawl may have stuck; "
                          "try a slower --crawl-vel or more --cogmap revs.".format(int(ok.sum())))

        # ---------------- DITHER validation: does HF torque dither dissolve the stiction? ----
        # The twitch is stick-slip: the velocity integrator winds against a STATIC-friction
        # deadband, then snaps free (releasing gearbox windup) and strikes the ~70 Hz mode. The
        # proposed fix is HF torque dither -- keep the contact micro-sliding so static friction
        # never re-establishes, so there is no deadband to wind against. CST has no velocity loop
        # so this can't be closed-loop tested from the host, but the MECHANISM premise can: hold
        # the rotor, superimpose HF dither on a slow DC-torque ramp, and ask whether breakaway
        # goes from a violent SNAP (deadband intact) to smooth CREEP (deadband dissolved). Two
        # numbers per amplitude: (1) residual motion from the dither ALONE = the "buzz" cost /
        # perceptibility; (2) the breakaway snap velocity = whether the deadband is gone. A* =
        # smallest amplitude that smooths breakaway; if its residual motion is imperceptible, the
        # window exists -> GO. Host SYNC is 1 kHz so dither >~250 Hz is coarsely sampled; firmware
        # dithers far faster (cleaner, smaller residual) -- so this is a CONSERVATIVE lower bound.
        if dither_amps:
            wd = 2 * np.pi * dither_hz / rate_hz
            R = max(1, dither_reps)
            if verbose:
                print("\n# DITHER validation: {:.0f} Hz torque dither vs stick-slip, amps {} per-mille, "
                      "{} reps each".format(dither_hz, dither_amps, R))
                print("#   (host 1 kHz SYNC caps clean dither ~250 Hz; firmware goes higher/cleaner)")
                print("#   Stick-slip snap scatters ~5x, so judge on the MEDIAN over reps, not one shot.")
                print("#   amp  ~mA   resid_ppk(max)  brk_DC(med)  snap med [min-max]     verdict")
            agg = {}
            for A in dither_amps:
                snaps = []; resids = []; brks = []
                for rep in range(R):
                    if not _settle_and_anchor():
                        continue
                    # --- phase 1: residual motion from dither ALONE (zero-mean -> no net drift) ---
                    st['dc_bias'] = 0.0; st['trq'] = float(A); st['dphase'] = wd; st['gain'] = 1.0
                    st['trim_on'] = False; st['abort'] = ''
                    st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
                    st['start'] = timer(); st['cap'] = True
                    t0 = time.time()
                    while time.time() - t0 < 1.0 and not st['abort']:
                        time.sleep(0.01)
                    st['cap'] = False
                    rpos = np.asarray(st['POS'], float)
                    res_pp = float(rpos.max() - rpos.min()) if rpos.size else 0.0
                    # --- phase 2: breakaway WITH dither running -- ramp DC bias until net motion ---
                    st['abort'] = ''
                    st['T'], st['POS'], st['VEL'], st['IQ'], st['TRQ'] = [], [], [], [], []
                    p_start = node.sdo['PositionFeedback'].raw
                    st['start'] = timer(); st['cap'] = True
                    found = None
                    for d in range(0, TRQ_AMP_CAP + 1):
                        st['dc_bias'] = float(d)             # dither (trq=A) still runs on top
                        time.sleep(0.03)
                        if st['abort']:
                            break
                        now = st['POS'][-1] if st['POS'] else p_start
                        if abs(now - p_start) > 12:
                            found = d; break
                    st['dc_bias'] = 0.0; st['trq'] = 0.0; st['gain'] = 0.0
                    node.rpdo[1]['TargetTorque'].raw = 0; st['cap'] = False
                    snap = max((abs(v) for v in st['VEL']), default=0)
                    if rep == 0:
                        _dump(csv_dir, "dith{}".format(A), st)
                    snaps.append(snap); resids.append(res_pp)
                    if found is not None:
                        brks.append(found)
                    if st['abort']:
                        _drop(node)
                if not snaps:
                    continue
                med = float(np.median(snaps)); mn = min(snaps); mx = max(snaps)
                max_res = max(resids); med_brk = float(np.median(brks)) if brks else None
                agg[A] = {'med': med, 'min': mn, 'max': mx, 'max_res': max_res, 'brk': med_brk}
                b = agg.get(0)
                if A == 0:
                    verd = "baseline (no dither)"
                elif max_res > 40:
                    verd = "dither too big -> drives the joint"
                elif b and med < 0.4 * b['med']:
                    verd = "SMOOTHED (deadband dissolving)"
                elif b and med < 0.75 * b['med']:
                    verd = "partial"
                else:
                    verd = "no better than baseline"
                if verbose:
                    print("  {:>4}  {:>4.0f}   {:>13.0f}  {:>10}  {:>6.0f} [{:.0f}-{:.0f}]   {}".format(
                        A, A * 1.414 * max_trq / kt, max_res,
                        "{:.0f}".format(med_brk) if med_brk is not None else "none",
                        med, mn, mx, verd))
                results.append({'mode': 'dither', 'amp': A, 'dither_hz': dither_hz, 'reps': R,
                                'snap_med': med, 'snap_min': mn, 'snap_max': mx,
                                'residual_pp_max': max_res, 'breakaway_dc_med': med_brk})
            # ----- overall verdict: a robust amplitude that smooths AND stays imperceptible? -----
            b0 = agg.get(0)
            wins = [(A, d) for A, d in agg.items()
                    if A > 0 and b0 and d['med'] < 0.4 * max(b0['med'], 1) and d['max_res'] < 20]
            if b0 and wins:
                A, d = min(wins, key=lambda x: x[0])
                out_deg = d['max_res'] / max(enc_res, 1) * 360.0 / max(gear, 1e-6)
                print("\n#  => DITHER VALIDATED: {} per-mille ({:.0f} mA) at {:.0f} Hz cuts the median "
                      "snap {:.0f} -> {:.0f} cts/s over {} reps,".format(
                          A, A * 1.414 * max_trq / kt, dither_hz, b0['med'], d['med'], R))
                print("#     with worst-case residual {:.0f} cts pk-pk motor = {:.4f} deg output "
                      "(/{:.1f} gear) = imperceptible. Window EXISTS -> GO for firmware.".format(
                          d['max_res'], out_deg, gear))
            elif b0 and any(A > 0 and d['med'] < 0.6 * b0['med'] for A, d in agg.items()):
                print("\n#  => PARTIAL: dither reduces the snap but not cleanly to the <40% bar, or the "
                      "residual creeps up. Window is narrow -- worth finer amps/higher freq before firmware.")
            elif b0:
                print("\n#  => NOT VALIDATED: no amplitude robustly smooths breakaway below scatter. "
                      "Raise dither freq (firmware can), or reconsider dither vs gain-down.")

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
    ap.add_argument('--ring', type=int, default=0, metavar='N',
                    help='RINGDOWN: N impulse-and-decay reps. Gives the mode damping directly '
                         'from the decay envelope, which small-signal sweeps cannot do on a '
                         'stiction-dominated plant.')
    ap.add_argument('--ring-ms', type=float, default=7.0,
                    help='impulse width ms (default 7 = half period at ~70 Hz, which puts the '
                         'energy on the mode we are hunting)')
    ap.add_argument('--ring-secs', type=float, default=0.6,
                    help='decay capture window s (default 0.6)')
    ap.add_argument('--breakaway', type=int, default=0, metavar='N',
                    help='BREAKAWAY: ramp torque until the rotor moves, N times. Quantifies the '
                         'stiction the limit cycle winds up against.')
    ap.add_argument('--cogmap', type=int, default=0, metavar='REVS',
                    help='COGGING/FRICTION MAP: crawl the rotor slowly under position control '
                         'through REVS electrical revolutions in both directions (no free snap) '
                         'and split the torque-vs-electrical-angle into cogging (conservative, '
                         'fwd+rev) and friction (dissipative, fwd-rev). Settles cogging-comp vs '
                         'dither. Try REVS=4.')
    ap.add_argument('--crawl-vel', type=float, default=200.0,
                    help='cogmap crawl speed cts/s (default 200). Slower = more quasi-static / '
                         'less inertial contamination but longer.')
    ap.add_argument('--dither', default=None, metavar='A0,A1,...',
                    help='DITHER validation: comma list of dither amplitudes (per-mille) to sweep, '
                         'e.g. "0,2,4,8,15". Include 0 for the no-dither baseline. Tests whether HF '
                         'torque dither dissolves the stick-slip deadband (snap->creep) and at what '
                         'residual "buzz" cost. Run with --vel-abort 25000 so the baseline snap is captured.')
    ap.add_argument('--dither-hz', type=float, default=250.0,
                    help='dither frequency Hz (default 250; host 1 kHz SYNC caps clean dither ~250).')
    ap.add_argument('--dither-reps', type=int, default=4,
                    help='reps per amplitude (default 4). Stick-slip snap scatters ~5x; the median '
                         'over reps is what separates the dither effect from that noise.')
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
    dither_amps = None
    if args.dither:
        dither_amps = [int(x) for x in args.dither.split(',')]
    if (not freqs and args.dc is None and not args.ring and not args.breakaway
            and not args.cogmap and not dither_amps):
        print("nothing to do: pass --sweep, --ring, --breakaway, --cogmap, --dither or --dc"); return 2

    res, st = run(args.can, args.node, rate_hz=args.rate, amp=args.amp, freqs=freqs,
                  dc=args.dc, secs=args.secs, cycles=args.cycles, csv_dir=args.csv_dir,
                  vel_abort=args.vel_abort, amp_exp=args.amp_exp, ring_n=args.ring,
                  ring_ms=args.ring_ms, ring_secs=args.ring_secs, brk_n=args.breakaway,
                  cogmap_revs=args.cogmap, crawl_vel=args.crawl_vel,
                  dither_amps=dither_amps, dither_hz=args.dither_hz, dither_reps=args.dither_reps)

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
