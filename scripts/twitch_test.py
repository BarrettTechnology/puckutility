#!/usr/bin/env python3
"""twitch_test.py -- dedicated HIGH-BANDWIDTH hunter for the Puck4 hold "twitch" (the
50-70 Hz velocity-loop limit-cycle seen during velocity-0 / position holds).

Why this exists
---------------
The Quick-Test-Matrix stability holds sample feedback over SDO (~135 Hz effective). The
limit-cycle sits at ~66 Hz+, i.e. right at that Nyquist -- so its frequency is unreliable and
may be ALIASED (the true ring could be higher). This tool instead uses the SYNC-driven PDO
stream (the exact mechanism the sinusoid soak uses) to sample position/velocity/current at
500-1000 Hz, so the ring is properly resolved.

What it does
------------
  * Holds the rotor (Cyclic-Sync-Position at a fixed setpoint) and captures at --rate Hz.
  * Optionally PERTURBS before each hold (a small position step "kick") to trigger the
    marginally-stable mode reliably -- the goal is catching it 10/10, not 3/18.
  * Repeats --trials times and QUANTIFIES each: dominant frequency (FFT of AC velocity),
    velocity/position/current oscillation amplitude, and whether it limit-cycled.
  * Reports a trigger RATE and the frequency/amplitude distribution, and saves a CSV trace of
    every triggered hold for plotting.
  * --gain-sweep: steps the Velocity Control Gain Factor (0x3024:4) and measures the ring
    amplitude at each -> direct evidence of which gain change kills it.

Standalone -- imports the QA harness's proven PDO/enable helpers from flywheel_test but does
NOT touch the Quick Test Matrix. Wire it in as a matrix step once it's dialed.
"""
import argparse
import os
import struct
import sys
import time
from timeit import default_timer as timer

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)                       # puckutility/ -- holds can_backend, puck4.eds
for _p in (ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import can_backend
from canopen_runner import MODE_IDLE, MODE_PROFILE_POS, MODE_CYCLIC_SYNC_POS
import flywheel_test as fw          # reuse CW_*/SW_*, _faulted, _home, EDS

EDS = fw.EDS
VEL_GAIN = (0x3024, 4)             # Velocity Control Gain Factor, 0.01 units
VEL_ZETA = (0x3024, 5)             # Velocity Zeta Damping Factor, 0.01 units


# --------------------------------------------------------------------------- #
def _analyze(t, vel, pos, iq, enc_res):
    """FFT the AC velocity; return dict of ring metrics (freq, amplitudes, current chatter)."""
    t = np.asarray(t, float); vel = np.asarray(vel, float)
    pos = np.asarray(pos, float); iq = np.asarray(iq, float)
    n = len(t)
    if n < 16:
        return None
    fs = n / (t[-1] - t[0]) if t[-1] > t[0] else 0.0
    v = vel - vel.mean()
    w = np.hanning(n)
    V = np.abs(np.fft.rfft(v * w))
    fr = np.fft.rfftfreq(n, 1.0 / fs) if fs else np.zeros(len(V))
    k = int(np.argmax(V[1:]) + 1) if len(V) > 1 else 0
    # POSITION-based twitch detection: 0x606C (velocity) is firmware encvel-LOWPASS filtered (0x2100:1),
    # so it ATTENUATES small ~60 Hz twitches -> vel_rms under-reads. Position (0x6064) is NOT filtered.
    # FFT the AC position, find the dominant peak ABOVE 15 Hz (skip slow drift), and measure spectral
    # COHERENCE (peak/median in-band) so a real limit-cycle trips but broadband quantization jitter does not.
    p = pos - pos.mean()
    P = np.abs(np.fft.rfft(p * w))
    pos_dom_freq = pos_coh = 0.0
    if fs and len(fr) > 2:
        inb = fr >= 15.0
        if inb.any():
            idx = np.where(inb)[0]
            kp = idx[int(np.argmax(P[idx]))]
            pos_dom_freq = float(fr[kp])
            med = float(np.median(P[idx]))
            pos_coh = float(P[kp] / med) if med > 1e-9 else 0.0
    return {
        'fs': fs,
        'n': n,
        'dom_freq': float(fr[k]) if k else 0.0,
        'vel_rms': float(np.std(v)),                       # cts/s AC (FILTERED velocity)
        'vel_rms_rpm': float(np.std(v) * 60.0 / enc_res),
        'pos_pkpk': float(pos.max() - pos.min()),          # cts
        'pos_ac_rms': float(np.std(p)),                    # cts AC (UNFILTERED position)
        'pos_dom_freq': pos_dom_freq,                      # Hz, dominant position tone >15 Hz
        'pos_coh': pos_coh,                                # peak/median in-band = coherence
        'iq_rms': float(np.std(iq)),                       # mA AC
        'iq_pk': float(np.abs(iq - iq.mean()).max()),
        'nyquist': fs / 2.0 if fs else 0.0,
    }


POS_AC_RMS_THR = 1.0     # cts; a coherent position wobble this small is still a visible/audible twitch
POS_COH_THR    = 5.0     # peak/median in-band; separates a real tone from quantization jitter


def _is_ring(a, vel_rms_thr, fmin, fmax):
    """A limit-cycle: a coherent in-band oscillation. Trips on EITHER the (firmware-filtered) velocity
    RMS OR an UNFILTERED-position coherent tone -- the latter catches small twitches the encvel lowpass
    hides in 0x606C (velocity), which otherwise read 'clean' while the rotor visibly buzzes."""
    if not a:
        return False
    vel_ring = a['vel_rms'] > vel_rms_thr and fmin <= a['dom_freq'] <= fmax
    pos_ring = (a.get('pos_ac_rms', 0.0) > POS_AC_RMS_THR and a.get('pos_coh', 0.0) > POS_COH_THR
                and fmin <= a.get('pos_dom_freq', 0.0) <= fmax)
    return bool(vel_ring or pos_ring)


# --------------------------------------------------------------------------- #
def run(can_device='can0', node_id=127, rate_hz=750.0, trials=10, hold_secs=2.5,
        perturb_cts=120, vel_rms_thr=600.0, fmin=15.0, fmax=None, csv_dir='twitchtests',
        gain_sweep=None, pos_sweep=None, out_sweep=None, repeat=1, enc_comp=None, settling=None,
        fric_ff=None, vel_kp=None, vel_ki=None, net=None, node=None, verbose=True):
    """Hunt the hold twitch. Returns (results_list, summary_dict).
    pos_sweep=N holds at N rotor positions across one mechanical revolution and reports the
    electrical angle (theta_e) of each -> tells cogging/detent (rings cluster at certain theta_e)
    from a pure loop limit-cycle (rings everywhere).
    Pass net+node to REUSE an existing connection (e.g. matrix_check owns the bus) -- then this
    does NOT open/close the network."""
    if fmax is None:
        fmax = rate_hz / 2.0 - 5.0
    _own_net = net is None
    if _own_net:
        net = can_backend.make_network(can_device, bitrate=1_000_000)
        node = None
    st = {'cap': False, 'target': 0, 'T': [], 'POS': [], 'VEL': [], 'IQ': [], 'start': 0.0,
          'emcy': 0}
    results = []
    orig_gain = None
    try:
        if node is None:
            node = net.add_node(node_id, EDS)
        node.sdo.RESPONSE_TIMEOUT = 2.0
        enc_res = node.sdo['EncoderConfig']['Resolution'].raw
        i_peak = node.sdo['Calibration']['i_peak'].raw
        st['i_peak'] = i_peak                               # for the id (d-axis) safety sampling
        gear_ratio = 1.0                                    # motor revs per output rev (0x6091:1/:2)
        try:
            _grm = float(node.sdo[0x6091][1].raw); _grs = float(node.sdo[0x6091][2].raw)
            if _grm > 0 and _grs > 0:
                gear_ratio = _grm / _grs
        except Exception:
            gear_ratio = 1.0
        try:
            poles = int(node.sdo[0x3011][3].raw)           # motor poles
            enc_zero = int(node.sdo[0x3011][1].raw)         # electrical zero, cts
        except Exception:
            poles, enc_zero = 14, 0
        cts_elec = enc_res / max(1.0, poles / 2.0)          # cts per electrical cycle
        # Optional: change the ADC settling time (0x3001:5, ns) and RE-CAL iSense bias+gain at
        # it -- the iSense ripple (a top twitch suspect) is settling-dependent, and bias/gain are
        # only valid at the settling they were cal'd for. Leaves the puck at the new settling +
        # cal (re-flash the config to restore); this is a diagnostic, not a config write.
        if settling is not None:
            try:
                _orig_set = int(node.sdo[0x3001][5].raw)
                node.sdo[0x3001][5].raw = int(settling)
                print("# MaxSettlingTime 0x3001:5 set {} ns (was {}) -- re-cal iSense at it...".format(
                    int(settling), _orig_set))
                import cli_ops
                ok1 = cli_ops._cli_calibrate_ibias(node, net)
                ok2 = cli_ops._cli_calibrate_igainfactor(node, net)
                print("# iSense re-cal at {} ns: bias {}  gain {}".format(
                    int(settling), "OK" if ok1 else "FAIL", "OK" if ok2 else "FAIL"))
            except Exception as e:
                print("# (settling / iSense re-cal failed: {})".format(e))
        # Optional: force encoder-harmonic compensation (0x3027:1) on/off to test whether the
        # theta_e-locked ring is encoder-nonlinearity-driven. Restored in finally.
        orig_enccomp = None
        if enc_comp is not None:
            try:
                orig_enccomp = int(node.sdo[0x3027][1].raw)
                node.sdo[0x3027][1].raw = int(enc_comp)
                print("# encoder comp (0x3027:1) forced {} (was {})".format(int(enc_comp), orig_enccomp))
            except Exception as e:
                print("# (could not set encoder comp: {})".format(e))
        # Optional: the RUNTIME velocity-loop knobs the firmware actually reads (motion.c) --
        #   0x2381:3 Coulomb/breakaway friction FF (trqdmd units +-1000; 0=off) -> the stiction-
        #     limit-cycle fix; 0x2381:1 vel Kp, 0x2381:2 vel Ki (U32). NOT 0x3024:4/5 (compute-only).
        # All restored in finally. Diagnostic writes; re-flash the config to fully restore.
        # raw SDO. _knobs stores (sub, width, prev_raw_int, name) for exact-bytes restore.
        _knobs = []
        # fric_ff 0x2381:3 = plain U16 integer (NOTE: absent on fw < the branch that added it -> write
        # will 0x06020000 and be skipped; also fades to 0 at standstill so it targets creep, not holds).
        if fric_ff is not None:
            try:
                _prev = int.from_bytes(node.sdo.upload(0x2381, 3), 'little')
                node.sdo.download(0x2381, 3, int(fric_ff).to_bytes(2, 'little'))
                print("# fric_ff (0x2381:3) set {} (was {})".format(int(fric_ff), _prev))
                _knobs.append((3, 2, _prev, 'fric_ff'))
            except Exception as e:
                print("# (could not set fric_ff: {})".format(e))
        # vel Kp/Ki 0x2381:1/2 = IEEE-754 float stored in a U32 (parseVelKp memcpy's bits into the live
        # PI struct). The arg is a SCALE factor: read float, x scale, write bits back. Restore = raw U32.
        for _scale, _sub, _name in ((vel_kp, 1, 'vel_kp'), (vel_ki, 2, 'vel_ki')):
            if _scale is None:
                continue
            try:
                _prev = int.from_bytes(node.sdo.upload(0x2381, _sub), 'little')
                _prevf = struct.unpack('<f', _prev.to_bytes(4, 'little'))[0]
                _newf = _prevf * float(_scale)
                node.sdo.download(0x2381, _sub, struct.pack('<f', _newf))
                print("# {} (0x2381:{}) x{}: {:.4g} -> {:.4g}".format(_name, _sub, _scale, _prevf, _newf))
                _knobs.append((_sub, 4, _prev, _name))
            except Exception as e:
                print("# (could not set {}: {})".format(_name, e))
        if verbose:
            print("# twitch_test node {}: enc_res={} i_peak={} mA  capture @ {:.0f} Hz "
                  "(Nyquist {:.0f} Hz)  {} trials x {:.1f}s  perturb={} cts".format(
                      node_id, enc_res, i_peak, rate_hz, rate_hz / 2.0, trials, hold_secs,
                      perturb_cts))

        # ---- PDO setup (mirrors flywheel_test) ----
        node.tpdo.read(); node.rpdo.read()
        if node.rpdo[1].cob_id is None:
            node.rpdo[1].cob_id = 0x200 + node_id
        if node.rpdo[2].cob_id is None:
            node.rpdo[2].cob_id = 0x300 + node_id

        def _data_cb(_msg):
            # Fires per SYNC-driven feedback frame: advance/hold the setpoint + latch a sample.
            try:
                if st.get('scan_dpos'):
                    st['target'] += st['scan_dpos']          # constant slow-velocity cogging scan
                node.rpdo[2]['TargetPosition'].raw = int(st['target'])
                if st['cap']:
                    st['T'].append(timer() - st['start'])
                    st['POS'].append(node.tpdo[1]['PositionFeedback'].raw)
                    st['VEL'].append(node.tpdo[2]['VelocityFeedback'].raw)
                    st['IQ'].append(node.tpdo[2]['CurrentFeedback'].raw / 1000.0 * i_peak)  # mA
            except Exception:
                pass

        def _emcy_cb(e):
            try:
                if e.code:
                    st['emcy'] = e.code
            except Exception:
                pass

        node.tpdo[2].add_callback(_data_cb)
        node.emcy.add_callback(_emcy_cb)
        node.sdo['HeartbeatPeriod'].raw = 0
        node.tpdo.save()

        # ---- enable + Cyclic-Sync-Position + home ----
        node.sdo['ControlWord'].raw = fw.CW_FAULTRESET
        node.sdo['ControlWord'].raw = fw.CW_SHUTDOWN
        node.sdo['ControlWord'].raw = fw.CW_ENABLE
        try:
            node.sdo['Cyclic']['InterpolationPeriod'].raw = int(1.0 / rate_hz * 1000)
            node.sdo['Cyclic']['InterpolationScale'].raw = -3
        except Exception:
            pass
        node.rpdo[1]['ControlWord'].raw = fw.CW_ENABLE
        node.rpdo[1]['SetModeOfOperation'].raw = MODE_CYCLIC_SYNC_POS
        if not fw._home(node):
            return results, {'error': 'drive faulted during homing'}

        home = node.sdo['PositionFeedback'].raw
        st['target'] = home
        node.rpdo[1].start(1.0 / rate_hz)
        node.rpdo[2].start(1.0 / rate_hz)
        net.sync.start(1.0 / rate_hz)
        time.sleep(0.4)

        # theta_e sweep: hold at N positions across a mech rev -> is the ring position-dependent?
        if pos_sweep:
            step = enc_res / float(pos_sweep)
            if verbose:
                print("# theta_e sweep: {} holds across 1 mech rev  (elec cycle {:.0f} cts, "
                      "{} poles, enc_zero {})".format(pos_sweep, cts_elec, poles, enc_zero))
            for i in range(pos_sweep):
                hp = int(home + i * step)
                a = _one_trial(node, st, hp, perturb_cts, hold_secs, enc_res, fw)
                ring = _is_ring(a, vel_rms_thr, fmin, fmax)
                meanpos = float(np.mean(st['POS'])) if st['POS'] else hp
                theta_e = ((meanpos - enc_zero) % cts_elec) / cts_elec * 360.0
                rec = {'pos': hp, 'theta_e': theta_e, 'ring': ring, **(a or {})}
                if ring:
                    rec['trace'] = _dump(csv_dir, "twitch_pos{}".format(i), st)
                results.append(rec)
                if verbose and a:
                    print("  pos {:>8} (theta_e {:>5.0f} deg): {:>6.1f} Hz  vel_rms {:>5.0f} cts/s "
                          "({:>4.1f} RPM)  iq {:>4.0f}  |id| {:>4.0f} mA  {}".format(
                              hp, theta_e, a['dom_freq'], a['vel_rms'], a['vel_rms_rpm'],
                              a.get('iq_pk', 0.0), a.get('id_abs_max_mA', 0.0),
                              "RING" if ring else "clean"))
                if st.get('runaway'):
                    print("  !! SAFETY ABORT ({}) -> drive dropped; ABORTING sweep. "
                          "The active gain/filter destabilised the loop.".format(
                              st.get('abort', 'unknown')))
                    break

        # OUTPUT-position sweep: hold across a FULL OUTPUT revolution (gear_ratio motor revs) to test
        # whether the twitch is GEARBOX-position-dependent (backlash/tooth-mesh -> rings cluster at
        # certain OUTPUT angles) vs a loop resonance (uniform). --pos-sweep only covers 1 MOTOR rev
        # = 1/gear_ratio of an output rev, so it CANNOT see gearbox-output effects. Big inter-point
        # jumps are slewed gradually (CSP step of thousands of cts would fault) with capture off.
        elif out_sweep:
            total = int(round(gear_ratio * enc_res))        # motor cts for 1 OUTPUT rev
            step = total / float(out_sweep)
            reps = max(1, int(repeat))
            if verbose:
                print("# OUTPUT sweep: {} holds x {} rep(s) across 1 OUTPUT rev = {} motor cts "
                      "({:.2f} motor revs, gear {:.2f}:1)  [same home all reps -> valid position "
                      "repeatability]".format(out_sweep, reps, total, gear_ratio, gear_ratio))
            for rep in range(reps):
                if verbose and reps > 1:
                    print("# --- rep {}/{} ---".format(rep + 1, reps))
                for i in range(out_sweep):
                    hp = int(home + i * step)
                    _slew_to(st, hp, cts_per_s=3 * enc_res, rate_hz=rate_hz)   # ramp target, cap off
                    a = _one_trial(node, st, hp, perturb_cts, hold_secs, enc_res, fw)
                    ring = _is_ring(a, vel_rms_thr, fmin, fmax)
                    meanpos = float(np.mean(st['POS'])) if st['POS'] else hp
                    out_deg = ((meanpos - home) / float(total) * 360.0) % 360.0   # OUTPUT shaft angle
                    theta_e = ((meanpos - enc_zero) % cts_elec) / cts_elec * 360.0
                    rec = {'pos': hp, 'idx': i, 'rep': rep, 'out_deg': out_deg,
                           'theta_e': theta_e, 'ring': ring, **(a or {})}
                    if ring:
                        rec['trace'] = _dump(csv_dir, "twitch_out{}_r{}".format(i, rep), st)
                    results.append(rec)
                    if verbose and a:
                        print("  out {:>5.1f} deg (theta_e {:>3.0f}): {:>6.1f} Hz  vel_rms {:>5.0f} "
                              "cts/s ({:>4.1f} RPM)  iq {:>4.0f}  |id| {:>4.0f} mA  {}".format(
                                  out_deg, theta_e, a['dom_freq'], a['vel_rms'], a['vel_rms_rpm'],
                                  a.get('iq_pk', 0.0), a.get('id_abs_max_mA', 0.0),
                                  "RING" if ring else "clean"))
                    if st.get('runaway'):
                        print("  !! SAFETY ABORT ({}) -> drive dropped; ABORTING sweep. "
                              "The active gain/filter destabilised the loop.".format(
                                  st.get('abort', 'unknown')))
                        break
                if st.get('runaway'):
                    break

        # optional gain sweep overrides the trial loop
        elif gain_sweep:
            orig_gain = node.sdo[VEL_GAIN[0]][VEL_GAIN[1]].raw
            if verbose:
                print("# gain sweep (orig VelGain={}): {}".format(orig_gain, gain_sweep))
            for g in gain_sweep:
                try:
                    node.sdo[VEL_GAIN[0]][VEL_GAIN[1]].raw = int(g)
                    rb = node.sdo[VEL_GAIN[0]][VEL_GAIN[1]].raw      # read back -> proves it applied
                except Exception as e:
                    print("  (could not set VelGain {}: {})".format(g, e)); continue
                time.sleep(0.4)
                # take the WORST of several trials (the ring is intermittent even at one gain)
                worst, worst_rms = None, -1.0
                for _ in range(3):
                    a = _one_trial(node, st, home, perturb_cts, hold_secs, enc_res, fw)
                    if a and a['vel_rms'] > worst_rms:
                        worst_rms, worst = a['vel_rms'], a
                ring = _is_ring(worst, vel_rms_thr, fmin, fmax)
                results.append({'gain': int(g), 'readback': rb, 'ring': ring, **(worst or {})})
                if verbose and worst:
                    warn = "" if rb == int(g) else "  <<WRITE DID NOT TAKE (rb={})".format(rb)
                    print("  VelGain set {:>4} (rb {:>4}): worst {:>6.1f} Hz  vel_rms {:>5.0f} cts/s "
                          "({:>4.1f} RPM)  iq_rms {:>4.0f} mA  {}{}".format(
                              int(g), rb, worst['dom_freq'], worst['vel_rms'], worst['vel_rms_rpm'],
                              worst['iq_rms'], "RING" if ring else "clean", warn))
        else:
            hit = 0
            for i in range(1, trials + 1):
                a = _one_trial(node, st, home, perturb_cts, hold_secs, enc_res, fw)
                ring = _is_ring(a, vel_rms_thr, fmin, fmax)
                hit += 1 if ring else 0
                rec = {'trial': i, 'ring': ring, **(a or {})}
                results.append(rec)
                if a and (ring or verbose):
                    tag = "RING" if ring else "clean"
                    p = ""
                    if ring:
                        p = _dump(csv_dir, "twitch_t{}".format(i), st)
                        rec['trace'] = p
                    print("  trial {:>2}: {:>6.1f} Hz (Nyq {:.0f})  vel_rms {:>5.0f} cts/s "
                          "({:>4.1f} RPM)  pos_pkpk {:>3.0f}  iq_rms {:>4.0f}  |id| {:>4.0f} mA  {}{}".format(
                              i, a['dom_freq'], a['nyquist'], a['vel_rms'], a['vel_rms_rpm'],
                              a['pos_pkpk'], a['iq_rms'], a.get('id_abs_max_mA', 0.0), tag,
                              "  ->" + os.path.basename(p) if p else ""))

        rings = [r for r in results if r.get('ring')]
        freqs = [r['dom_freq'] for r in rings if r.get('dom_freq')]
        summary = {
            'trials': len(results),
            'rings': len(rings),
            'rate_hz': rate_hz,
            'freq_mean': float(np.mean(freqs)) if freqs else 0.0,
            'freq_min': float(np.min(freqs)) if freqs else 0.0,
            'freq_max': float(np.max(freqs)) if freqs else 0.0,
            'vel_rms_max': float(max((r.get('vel_rms', 0) for r in rings), default=0.0)),
        }
        return results, summary
    finally:
        try:
            if orig_gain is not None:
                node.sdo[VEL_GAIN[0]][VEL_GAIN[1]].raw = int(orig_gain)
                print("# restored VelGain to {}".format(orig_gain))
        except Exception:
            pass
        try:
            if orig_enccomp is not None:
                node.sdo[0x3027][1].raw = int(orig_enccomp)
                print("# restored encoder comp (0x3027:1) to {}".format(orig_enccomp))
        except Exception:
            pass
        for _sub, _w, _orig, _name in (locals().get('_knobs') or []):
            try:
                node.sdo.download(0x2381, _sub, int(_orig).to_bytes(_w, 'little'))
                print("# restored {} (0x2381:{}) to {}".format(_name, _sub, _orig))
            except Exception:
                pass
        try:
            net.sync.stop(); node.rpdo[1].stop(); node.rpdo[2].stop()
        except Exception:
            pass
        # CRITICAL: remove OUR callbacks so a shared net/node (velki_sweep, matrix_check) doesn't
        # accumulate stale callbacks that keep writing conflicting TargetPositions on later runs.
        for _cbname, _owner in (('_data_cb', 'tpdo'), ('_emcy_cb', 'emcy')):
            try:
                _cb = locals().get(_cbname)
                if _cb is not None and node is not None:
                    (node.tpdo[2] if _owner == 'tpdo' else node.emcy).callbacks.remove(_cb)
            except Exception:
                pass
        try:
            if node is not None:
                node.sdo.RESPONSE_TIMEOUT = 2.0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
        except Exception:
            pass
        try:
            if _own_net:
                net.disconnect()
        except Exception:
            pass


def _drop_drive(node):
    """Drop the drive NOW. Used by every safety guard; failures are ignored on
    purpose -- if the bus is sick, the caller still has to stop the sweep."""
    try:
        node.sdo['SetModeOfOperation'].raw = MODE_IDLE
        node.sdo['ControlWord'].raw = 0x06                 # shutdown
    except Exception:
        pass


RUNAWAY_CTS_S = 30000        # ~440 rpm motor; normal twitch peaks ~5k -> a divergence guard, not a ring

# CURRENT GUARDS, as fractions of the puck's own i_peak so they scale across models.
# The velocity guard above catches a loop that diverges in SPEED, but a destabilising
# filter can also pin the current without much motion -- a lowpass in the velocity loop
# once produced a 3000 RPM runaway at 35 Hz, and a mis-commutated angle can drive id
# hard negative (the 2026-08-04 magnet-burst case) with the rotor barely turning.
# Neither shows up as excess velocity, so speed alone is not enough.
#
# Observed NORMAL during a twitch on a P4-32: iq 0-600 mA, |id| 0-120 mA, against an
# i_peak of 28284 mA. These ceilings sit ~10-20x above that and far below anything
# destructive -- they exist to stop a bad filter early, not to replace the drive's i2t.
IQ_ABORT_FRAC = 0.20         # 20% of i_peak
ID_ABORT_FRAC = 0.10         # tighter: FOC commands id=0, so any real id is already wrong


def _slew_to(st, target, cts_per_s, rate_hz):
    """Ramp st['target'] to `target` at a bounded rate (capture off) so a large output-sweep jump
    doesn't step the CSP setpoint by thousands of cts (which would fault). The SYNC callback ships
    st['target'] each cycle; we just advance it smoothly here."""
    st['cap'] = False
    cur = float(st['target'])
    dist = target - cur
    step = max(1.0, cts_per_s / max(1.0, rate_hz))
    n = int(abs(dist) / step) + 1
    d = dist / n
    for _ in range(n):
        cur += d
        st['target'] = int(cur)
        time.sleep(1.0 / rate_hz)
    st['target'] = int(target)
    time.sleep(0.3)                                          # let it arrive/settle before the trial


def _one_trial(node, st, hold_pos, perturb_cts, hold_secs, enc_res, fw, settle=0.5):
    """Move to hold_pos, settle, perturb (kick) then hold; capture at PDO rate; return metrics."""
    hold_pos = int(hold_pos)
    st['target'] = hold_pos
    time.sleep(settle)                                    # slew to / settle at the hold position
    # kick: step off briefly to excite the marginally-stable mode, then command back
    if perturb_cts:
        st['target'] = hold_pos + int(perturb_cts)
        time.sleep(0.15)
        st['target'] = hold_pos
        time.sleep(0.15)
    # capture the hold
    st['T'], st['POS'], st['VEL'], st['IQ'], st['ID'] = [], [], [], [], []
    st['start'] = timer()
    st['runaway'] = False
    st['abort'] = ''
    st['cap'] = True
    t0 = time.time()
    i_peak = st.get('i_peak', 0) or 0
    _k = 0
    _iq_seen = 0
    while time.time() - t0 < hold_secs:
        if fw._faulted(node):
            break
        # RUNAWAY GUARD: a destabilising gain/filter can diverge the loop to 1000s of RPM without
        # tripping the drive's vel-track fault (it oscillates AROUND the hold). Drop the drive the
        # instant |vel| exceeds a safe ceiling, and flag the abort so the sweep stops.
        if st['VEL'] and abs(st['VEL'][-1]) > RUNAWAY_CTS_S:
            st['runaway'] = True
            st['abort'] = 'vel {:.0f} cts/s > {}'.format(st['VEL'][-1], RUNAWAY_CTS_S)
            _drop_drive(node)
            break
        # CURRENT GUARD (q-axis): a destabilised loop can pin torque without ever
        # exceeding the speed ceiling -- it oscillates hard against the gearbox.
        # This loop turns at ~100 Hz but iq arrives at the PDO rate (~750 Hz), so
        # check every sample since the last pass rather than only the newest one:
        # a spike that lands and subsides between iterations still trips the guard.
        if i_peak:
            _tail = st['IQ'][_iq_seen:]
            if _tail:
                _iq_seen += len(_tail)
                _pk = max(_tail, key=abs)
                if abs(_pk) > IQ_ABORT_FRAC * i_peak:
                    st['runaway'] = True
                    st['abort'] = 'iq {:+.0f} mA, |iq| > {:.0f} mA ({:.0%} of i_peak)'.format(
                        _pk, IQ_ABORT_FRAC * i_peak, IQ_ABORT_FRAC)
                    _drop_drive(node)
                    break
        # SAFETY: sample id (d-axis current, 0x3010:6, per-mille of i_peak) ~30 Hz. FOC commands id=0,
        # so |id| staying tiny during the twitch proves clean commutation + no wasted/heating current.
        _k += 1
        if _k % 3 == 0 and i_peak:
            try:
                _idr = int.from_bytes(node.sdo.upload(0x3010, 6), 'little', signed=True)
                _idma = _idr / 1000.0 * i_peak                     # mA
                st['ID'].append(_idma)
                # CURRENT GUARD (d-axis): FOC commands id=0, so a large |id| means the
                # commutation angle is wrong or the voltage vector inverted -- the
                # regime that burns magnets. Guarded tighter than iq for that reason.
                if abs(_idma) > ID_ABORT_FRAC * i_peak:
                    st['runaway'] = True
                    st['abort'] = 'id {:+.0f} mA, |id| > {:.0f} mA ({:.0%} of i_peak)'.format(
                        _idma, ID_ABORT_FRAC * i_peak, ID_ABORT_FRAC)
                    _drop_drive(node)
                    break
            except Exception:
                pass
        time.sleep(0.01)
    st['cap'] = False
    a = _analyze(st['T'], st['VEL'], st['POS'], st['IQ'], enc_res)
    if a is not None and st['ID']:
        idv = np.asarray(st['ID'], float)
        a['id_mean_mA'] = float(idv.mean())
        a['id_abs_max_mA'] = float(np.max(np.abs(idv)))            # worst |d-axis current| in the hold
        a['id_ac_rms_mA'] = float(np.std(idv))
    return a


def _dump(csv_dir, tag, st):
    os.makedirs(csv_dir, exist_ok=True)
    fp = os.path.join(csv_dir, "{}_{}.csv".format(tag, time.strftime("%Y%m%d-%H%M%S")))
    with open(fp, 'w') as fh:
        fh.write("t_s,pos_cts,vel_cts_s,iq_mA\n")
        for i in range(len(st['T'])):
            fh.write("{:.5f},{:d},{:.1f},{:.1f}\n".format(
                st['T'][i], int(st['POS'][i]), st['VEL'][i], st['IQ'][i]))
    return fp


def main():
    # Safety thresholds are module globals so the capture loop reads them without threading
    # them through run(); the CLI only widens/narrows what the guards already enforce.
    global IQ_ABORT_FRAC, ID_ABORT_FRAC, RUNAWAY_CTS_S
    ap = argparse.ArgumentParser(description="High-bandwidth Puck4 hold-twitch hunter.")
    ap.add_argument('--can', default='can0')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--rate', type=float, default=750.0, help='PDO capture rate Hz (default 750)')
    ap.add_argument('--trials', type=int, default=10, help='hold trials (default 10)')
    ap.add_argument('--hold-secs', type=float, default=2.5, help='capture per trial s (default 2.5)')
    ap.add_argument('--perturb-cts', type=int, default=120,
                    help='perturbation kick in motor cts before each hold (0 = none; default 120)')
    ap.add_argument('--vel-rms-thr', type=float, default=600.0,
                    help='vel-RMS cts/s above which a hold counts as ringing (default 600 ~9 RPM)')
    ap.add_argument('--fmin', type=float, default=15.0, help='ring band low Hz (default 15)')
    ap.add_argument('--fmax', type=float, default=None, help='ring band high Hz (default Nyquist-5)')
    ap.add_argument('--csv-dir', default='twitchtests')
    ap.add_argument('--gain-sweep', default=None,
                    help='comma-separated VelGain values to sweep (0x3024:4), e.g. 100,80,60,40')
    ap.add_argument('--pos-sweep', type=int, default=None,
                    help='hold at N rotor positions across 1 mech rev and report theta_e of each '
                         '-> tells cogging/detent (rings cluster at certain theta_e) from a pure '
                         'loop limit-cycle (rings everywhere), e.g. 24')
    ap.add_argument('--out-sweep', type=int, default=None,
                    help='hold at N positions across 1 full OUTPUT revolution (gear_ratio motor revs) '
                         '-> is the twitch GEARBOX-position-dependent (rings cluster at certain output '
                         'angles = backlash/tooth-mesh) vs a loop resonance (uniform)? e.g. 36')
    ap.add_argument('--repeat', type=int, default=1,
                    help='repeat the --out-sweep grid this many times from the SAME home -> per-position '
                         'ring repeatability: DETERMINISTIC (same angles ring every rep = mechanical) vs '
                         'STOCHASTIC (random angles = resonance triggered by chance). e.g. 3')
    ap.add_argument('--enc-comp', type=int, default=None, choices=[0, 1],
                    help='force encoder-harmonic comp 0x3027:1 on(1)/off(0) for this run (restored '
                         'after) -> run a --pos-sweep with each to test the encoder-nonlinearity cause')
    ap.add_argument('--settling', type=int, default=None,
                    help='set ADC MaxSettlingTime 0x3001:5 (ns) and re-cal iSense at it, then sweep '
                         '-> test the iSense-ripple cause (e.g. 800). Leaves settling+cal changed.')
    ap.add_argument('--fric-ff', type=int, default=None,
                    help='set Coulomb/breakaway friction FF 0x2381:3 (trqdmd units +-1000; 0=off). NOTE: '
                         'absent on fw < the branch that added it, and fades to 0 at standstill (targets '
                         '1-5 rpm creep, not holds). Restored after.')
    ap.add_argument('--vel-kp', type=float, default=None,
                    help='SCALE the RUNTIME velocity Kp 0x2381:1 (live PI gain) by this factor for this '
                         'run, e.g. 0.5 -> test if lowering loop gain kills the ~70 Hz hold limit cycle. '
                         'Restored after.')
    ap.add_argument('--vel-ki', type=float, default=None,
                    help='SCALE the RUNTIME velocity Ki 0x2381:2 (live PI gain) by this factor, e.g. 0.5. '
                         'Restored after.')
    ap.add_argument('--iq-abort-frac', type=float, default=IQ_ABORT_FRAC,
                    help='SAFETY: abort the sweep if |iq| exceeds this fraction of i_peak '
                         '(default {:.2f}). Raise only with a reason.'.format(IQ_ABORT_FRAC))
    ap.add_argument('--id-abort-frac', type=float, default=ID_ABORT_FRAC,
                    help='SAFETY: abort the sweep if |id| exceeds this fraction of i_peak '
                         '(default {:.2f}). FOC commands id=0, so keep this tight.'.format(ID_ABORT_FRAC))
    ap.add_argument('--runaway-cts', type=float, default=RUNAWAY_CTS_S,
                    help='SAFETY: abort if |vel| exceeds this (cts/s, default {}).'.format(RUNAWAY_CTS_S))
    args = ap.parse_args()

    IQ_ABORT_FRAC, ID_ABORT_FRAC = args.iq_abort_frac, args.id_abort_frac
    RUNAWAY_CTS_S = args.runaway_cts
    print("# safety guards: |vel| < {:.0f} cts/s, |iq| < {:.0%} i_peak, |id| < {:.0%} i_peak".format(
        RUNAWAY_CTS_S, IQ_ABORT_FRAC, ID_ABORT_FRAC))

    gs = [float(x) for x in args.gain_sweep.split(',')] if args.gain_sweep else None
    results, summary = run(args.can, args.node, rate_hz=args.rate, trials=args.trials,
                           hold_secs=args.hold_secs, perturb_cts=args.perturb_cts,
                           vel_rms_thr=args.vel_rms_thr, fmin=args.fmin, fmax=args.fmax,
                           csv_dir=args.csv_dir, gain_sweep=gs, pos_sweep=args.pos_sweep,
                           out_sweep=args.out_sweep, repeat=args.repeat,
                           enc_comp=args.enc_comp, settling=args.settling,
                           fric_ff=args.fric_ff, vel_kp=args.vel_kp, vel_ki=args.vel_ki)
    print("\n==== TWITCH SUMMARY ====")
    if 'error' in summary:
        print("ERROR:", summary['error']); return 2
    if args.out_sweep:
        # dump per-(position,rep) data for graphing (plot_outsweep.py)
        import json
        recs = [{k: r.get(k) for k in ('idx', 'rep', 'out_deg', 'theta_e', 'ring',
                                       'dom_freq', 'vel_rms', 'vel_rms_rpm',
                                       'pos_ac_rms', 'pos_dom_freq', 'pos_coh',
                                       'iq_pk', 'id_abs_max_mA', 'id_ac_rms_mA')}
                for r in results if 'idx' in r]
        dump = {'node': args.node, 'out_sweep': args.out_sweep, 'repeat': args.repeat,
                'vel_rms_thr': args.vel_rms_thr, 'records': recs}
        try:
            os.makedirs(args.csv_dir, exist_ok=True)
            path = os.path.join(args.csv_dir, 'outsweep_node{}_{}pts_{}rep.json'.format(
                args.node, args.out_sweep, args.repeat))
            with open(path, 'w') as f:
                json.dump(dump, f)
            print("# out-sweep data -> {}  (graph: scripts/plot_outsweep.py {})".format(path, path))
        except Exception as e:
            print("# (could not dump out-sweep data: {})".format(e))
    if args.pos_sweep:
        rings = [r for r in results if r.get('ring')]
        print("rang at {}/{} rotor positions".format(len(rings), len(results)))
        if rings:
            tes = sorted(r['theta_e'] for r in rings)
            print("ring electrical angles (theta_e): " + ", ".join("{:.0f}".format(t) for t in tes))
            # largest CLEAN arc between consecutive ring angles (wrapping 360). A big clean arc
            # means the rings concentrate in the rest of the cycle -> theta_e-locked.
            gaps = [tes[i + 1] - tes[i] for i in range(len(tes) - 1)] + [360.0 - tes[-1] + tes[0]]
            clean_arc = max(gaps)
            ring_arc = 360.0 - clean_arc
            if clean_arc > 100.0 and len(rings) < len(results):
                print(">>> rings CONCENTRATE in a {:.0f} deg arc (largest clean gap {:.0f} deg) -> "
                      "theta_e-LOCKED -> COGGING / DETENT-driven. Fix: cogging / encoder-harmonic "
                      "comp (0x3027) to flatten the torque ripple; gain/damping only masks it."
                      .format(ring_arc, clean_arc))
            else:
                print(">>> rings spread evenly across theta_e (largest clean gap {:.0f} deg) -> "
                      "position-INDEPENDENT -> velocity-loop / estimator limit-cycle "
                      "(fix: gain/damping/filtering).".format(clean_arc))
    elif args.out_sweep and args.repeat and args.repeat > 1:
        # per-position repeatability across reps (same home -> same absolute output angles)
        reps = args.repeat
        byidx = {}
        for r in results:
            byidx.setdefault(r['idx'], []).append(r)
        print("OUTPUT-position repeatability across {} reps  (RING count per position):".format(reps))
        print("  {:>4} {:>8} {:>8}  {:<10} {}".format("idx", "out_deg", "rings", "verdict", "vel_rms each rep"))
        determ, stoch = [], []
        for i in sorted(byidx):
            rs = sorted(byidx[i], key=lambda r: r['rep'])
            nring = sum(1 for r in rs if r.get('ring'))
            od = rs[0]['out_deg']
            vs = " ".join("{:>4.0f}".format(r.get('vel_rms', 0)) for r in rs)
            if nring == reps:
                verdict = "DETERMIN"; determ.append(od)
            elif nring == 0:
                verdict = "clean"
            else:
                verdict = "stochastic"; stoch.append(od)
            if nring:
                print("  {:>4} {:>7.0f}  {:>3}/{}   {:<10} {}".format(i, od, nring, reps, verdict, vs))
        print("\n  DETERMINISTIC (rang every rep): {} -> {}".format(
            len(determ), ", ".join("{:.0f}".format(o) for o in sorted(determ)) or "(none)"))
        print("  STOCHASTIC (rang some reps):    {} -> {}".format(
            len(stoch), ", ".join("{:.0f}".format(o) for o in sorted(stoch)) or "(none)"))
        if len(stoch) > len(determ):
            print(">>> MOSTLY STOCHASTIC -> the ~80 Hz limit cycle is always lurking; whether a hold "
                  "falls into it is chance (perturbation vs basin), NOT a fixed output/gearbox position "
                  "-> loop/torsional RESONANCE, not backlash. Fix: notch / lower bandwidth / stiffen.")
        elif determ:
            print(">>> DETERMINISTIC SUBSET rings every time -> those OUTPUT angles are a real mechanical "
                  "feature (backlash / tooth-mesh zones). Map + address those positions.")
    elif args.out_sweep:
        rings = [r for r in results if r.get('ring')]
        print("rang at {}/{} OUTPUT positions".format(len(rings), len(results)))
        if rings:
            ods = sorted(r['out_deg'] for r in rings)
            print("ring OUTPUT angles (deg): " + ", ".join("{:.0f}".format(o) for o in ods))
            gaps = [ods[i + 1] - ods[i] for i in range(len(ods) - 1)] + [360.0 - ods[-1] + ods[0]]
            clean_arc = max(gaps)
            if clean_arc > 100.0 and len(rings) < len(results):
                print(">>> rings CLUSTER at certain OUTPUT angles (largest clean gap {:.0f} deg) -> "
                      "GEARBOX-position-dependent (backlash / tooth-mesh zones). Motor+gearbox specific."
                      .format(clean_arc))
            else:
                print(">>> rings spread evenly across the OUTPUT revolution (largest clean gap {:.0f} "
                      "deg) -> NOT gearbox-position-dependent -> a loop/torsional RESONANCE independent "
                      "of output angle (fix: notch / lower bandwidth / stiffen, not backlash).".format(clean_arc))
    elif not args.gain_sweep:
        print("triggered {}/{} holds  ({:.0f}%)".format(
            summary['rings'], summary['trials'],
            100.0 * summary['rings'] / max(1, summary['trials'])))
        if summary['rings']:
            print("ring frequency: {:.1f} Hz (range {:.1f}-{:.1f})  capture {:.0f} Hz".format(
                summary['freq_mean'], summary['freq_min'], summary['freq_max'], summary['rate_hz']))
            print("worst velocity ripple: {:.0f} cts/s".format(summary['vel_rms_max']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
