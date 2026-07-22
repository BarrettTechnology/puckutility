#!/usr/bin/env python3
"""Top-speed VITALS -- PEAK vs CONTINUOUS top speed, and whether the i2t cut the peak SHORT.

A motor stuck below its rated speed usually has two top speeds:
  * PEAK       -- the burst right after spin-up, current still above i_cont.
  * CONTINUOUS -- what's left after the i2t folds current back (what you can hold).

CRUCIAL extra: at the instant the i2t folds the current, is the RPM still CLIMBING or has it PLATEAUED?
  * slope ~= 0  -> the peak is the motor's REAL ceiling (voltage/torque-limited); i2t isn't costing peak.
  * slope  > 0  -> the i2t cut it off EARLY -- you're leaving speed on the table; delaying the fold
                   (raise i_cont / TempLimitedContinuousCurrent) would let it climb higher first.

We command the ceiling, log the whole spin-up->fold trajectory (RPM, iq, i2t, |V|, bus) until the i2t
accumulator (0x3025:1) plateaus, then extract PEAK, CONTINUOUS, and the RPM slope at fold-back.

Usage:   scripts/top_speed_vitals.py [can_device] [node_id] [--settling <ns>]   (defaults: can0 127)
         --settling overrides MaxSettlingTime (0x3001:5) for the run, restored on exit, so you can A/B
         the measured current vs the current-sense settling time (does a low settle inflate iq?).
Safety:  spins the motor to its velocity CEILING for ~10-20 s; drive idled on exit / Ctrl-C.
"""
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend
from canopen_runner import CLEAR_FAULT, SHUTDOWN, OP_ENABLED, MODE_IDLE, MODE_PROFILE_VEL

# Pull --settling <ns> (or --settling=<ns>) out of argv; the rest are positional can_device / node_id.
_argv = sys.argv[1:]
SETTLING = None
_i = 0
while _i < len(_argv):
    if _argv[_i] == '--settling' and _i + 1 < len(_argv):
        SETTLING = int(_argv[_i + 1]); del _argv[_i:_i + 2]; continue
    if _argv[_i].startswith('--settling='):
        SETTLING = int(_argv[_i].split('=', 1)[1]); del _argv[_i]; continue
    _i += 1
CAN  = _argv[0] if len(_argv) > 0 else 'can0'
NODE = int(_argv[1]) if len(_argv) > 1 else 127
EDS  = os.path.join(ROOT, 'puck4.eds')

SOAK_MAX  = 20.0     # hard cap on the log/soak (i2t should plateau well under this)
MIN_LOG   = 5.0      # log at least this long so a fold (if any) is captured
FULL_MOD  = 32000.0  # ud/uq full-scale
WIN_S     = 1.2      # window for the fold-slope fit + the peak/continuous stats
COAST_TIMEOUT = 20.0 # max seconds to log the freewheel coast-down
COAST_FLOOR   = 0.15 # stop the coast log once RPM decays below this fraction of the coast-start speed
I2T_READY     = 300  # spin up only once the i2t accumulator (0x3025:1) has decayed below this -- a hot
                     # accumulator left by a recent run folds current immediately and truncates the peak
I2T_WAIT_MAX  = 60.0 # cap on the i2t bleed-off wait (0x3025:1 is read-only; it only decays with idle time)


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _std(a):
    if len(a) < 2:
        return 0.0
    m = _mean(a)
    return (sum((x - m) ** 2 for x in a) / len(a)) ** 0.5


def _slope(ts, ys):
    """Least-squares slope dy/dt over paired samples (units of y per second)."""
    n = len(ts)
    if n < 3:
        return 0.0
    tm, ym = _mean(ts), _mean(ys)
    den = sum((t - tm) ** 2 for t in ts)
    if den <= 0:
        return 0.0
    return sum((ts[i] - tm) * (ys[i] - ym) for i in range(n)) / den


def _win(T, Y, t_lo, t_hi):
    return [Y[i] for i in range(len(T)) if t_lo <= T[i] <= t_hi]


def vitals(node):
    i_peak = node.sdo['Calibration']['i_peak'].raw
    enc_res = node.sdo['EncoderConfig']['Resolution'].raw
    try:
        i_cont = int.from_bytes(node.sdo.upload(0x3011, 8), 'little', signed=False)
    except Exception:
        i_cont = 0
    try:
        i_templim = int.from_bytes(node.sdo.upload(0x3025, 3), 'little', signed=False)
    except Exception:
        i_templim = 0

    def _u(idx, sub, signed=False, default=0):
        try:
            return int.from_bytes(node.sdo.upload(idx, sub), 'little', signed=signed)
        except Exception:
            return default
    poles   = _u(0x3011, 3)                 # motor pole COUNT
    kt_raw  = _u(0x3011, 4)                 # torque constant (raw OD units)
    rt_raw  = _u(0x3011, 5)                 # winding resistance (raw OD units)
    lt_raw  = _u(0x3011, 6)                 # inductance (raw OD units)
    j_raw   = _u(0x3011, 7)                 # rotor inertia (raw OD units)
    no_load = _u(0x3024, 6)                 # NoLoadSpeed (RPM) -- the puck's own top-speed spec
    pwm_hz  = _u(0x3001, 1)                 # PWM freq (Hz) -- verify forced/tuned rate took effect
    pole_pairs = (poles // 2) if poles else 0

    def _motor_temp():
        return _u(0x3000, 2, signed=True, default=None)
    temp0 = _motor_temp()
    try:
        set_ns = node.sdo['Amp']['MaxSettlingTime'].raw           # current-sense settling (ns), applied live
    except Exception:
        set_ns = -1

    try:
        _code = int(node.sdo[0x1018][2].raw)
        model = can_backend.model_from_product_code(_code) or "puck{}".format(_code)
    except Exception:
        model = "puck"
    try:
        lag = int(node.sdo['EncoderConfig']['LagFactor'].raw)
    except Exception:
        lag = -1
    try:
        max_vel = int(node.sdo['max_velocity'].raw)
    except Exception:
        max_vel = 0
    if max_vel <= 0:
        max_vel = int(round(15000.0 / 60.0 * enc_res))

    node.sdo['SetModeOfOperation'].raw = MODE_IDLE

    # Bleed off a hot i2t accumulator (leftover from a recent run) before spinning up -- otherwise it
    # folds current immediately and truncates the peak. 0x3025:1 is read-only, so it only decays with
    # idle time (~20/s); wait (idle) until it's below I2T_READY or we hit the cap.
    def _i2t_now():
        try:
            return node.sdo[0x3025][1].raw
        except Exception:
            return 0
    i2t_start = _i2t_now()
    i2t_waited = 0.0
    if i2t_start > I2T_READY:
        print("Waiting for i2t to bleed off ({} -> <{}) so the peak isn't truncated...".format(
            i2t_start, I2T_READY))
        print("  (i2t models WINDING COOLING -- decays only while idle, ~20/s. A high start here"
              " usually means the puck was just driven hard, e.g. an enc-comp hunt.)")
        _wt0 = time.time()
        _last = None; _last_t = None
        while (time.time() - _wt0) < I2T_WAIT_MAX:
            _v = _i2t_now()
            if _v <= I2T_READY:
                break
            _el = time.time() - _wt0
            # live countdown with an ETA from the observed decay rate, so a STUCK accumulator (rate ~0
            # = not cooling) is obvious rather than looking like a hang.
            _rate = ((_last - _v) / (_el - _last_t)) if (_last is not None and _el > _last_t) else 0.0
            _eta = ((_v - I2T_READY) / _rate) if _rate > 0.1 else float('inf')
            print("  i2t = {:>5}  ({:.0f}s elapsed, {:.0f}/s{})".format(
                _v, _el, _rate,
                ", ~{:.0f}s left".format(_eta) if _eta != float('inf') else " -- NOT decaying?!"))
            # ABORT if it's not bleeding after ~10 s (flat or RISING). A rising accumulator at IDLE
            # means the firmware sees current > i_cont with the motor OFF -- a PHANTOM idle current from
            # an incomplete current-sense cal. i2t will never bleed, and it FALSELY current-limits the
            # motor, so a run now would be capped/invalid (peak folded to i_cont). Stop and say why.
            if _el >= 10.0 and _v >= i2t_start - 3:
                # MEASURE the idle current to confirm/quantify the phantom (motor is idle, so |I| must
                # be ~0). A large idle |I| is the smoking gun: a bad current-sense zero (bias/offset).
                try:
                    _idle_id = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                    _idle_iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                    _idle_i = (_idle_id * _idle_id + _idle_iq * _idle_iq) ** 0.5
                except Exception:
                    _idle_i = float('nan')
                print("\n  !! i2t is NOT bleeding (idle) -- flat/RISING. The firmware senses current > "
                      "i_cont ({} mA) with the motor OFF: a PHANTOM idle current.".format(i_cont))
                print("     measured idle |I| = {:.0f} mA  (should be ~0)  <-- bad current-sense zero "
                      "(bias/offset).".format(_idle_i))
                print("     The motor is NOT hot; this falsely trips i2t and current-limits the motor, "
                      "so top speed would be capped at CONTINUOUS, not the real peak. A run now is invalid.")
                print("     FIX: re-run the full current-sense calibration until idle |I| (0x3010:6 / "
                      "0x6078) reads ~0. Aborting.")
                node.sdo['TargetVelocity'].raw = 0
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                raise SystemExit(1)
            _last, _last_t = _v, _el
            time.sleep(2.0)
        i2t_waited = time.time() - _wt0
        i2t_start = _i2t_now()
        print("  i2t now {} (waited {:.0f}s)".format(i2t_start, i2t_waited))

    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo['ControlWord'].raw = cw
    node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
    print("Commanding {} cts/s (~{:.0f} RPM ceiling); logging spin-up -> fold...".format(
        max_vel, max_vel * 60.0 / enc_res))
    node.sdo['TargetVelocity'].raw = int(max_vel)

    # --- log the whole trajectory until the i2t accumulator plateaus (equilibrium) ---
    T, RPM, IQ, ID, I2, VF, BUS = [], [], [], [], [], [], []
    t0 = time.time()
    hist = []
    while True:
        now = time.time() - t0
        rpm = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
        iq = node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
        idc = node.sdo['Motor']['id'].raw / 1000.0 * i_peak
        ud = node.sdo['Motor']['ud'].raw
        uq = node.sdo['Motor']['uq'].raw
        try:
            i2 = node.sdo[0x3025][1].raw
        except Exception:
            i2 = 0
        bus = node.sdo['Amplifier']['BusVoltage'].raw / 10.0
        T.append(now); RPM.append(rpm); IQ.append(iq); ID.append(idc)
        I2.append(i2); VF.append((ud * ud + uq * uq) ** 0.5 / FULL_MOD * 100.0); BUS.append(bus)
        # stop when the i2t accumulator has plateaued (past MIN_LOG), or at SOAK_MAX
        hist.append((now, i2))
        hist = [(t, v) for t, v in hist if now - t <= 2.5]
        if now > MIN_LOG and len(hist) >= 5:
            vals = [v for _, v in hist]
            if (max(vals) - min(vals)) <= max(2.0, 0.03 * abs(_mean(vals))):
                break
        if now >= SOAK_MAX:
            break

    # --- COAST-DOWN: cut the bridge to high-Z and log the freewheel. With no winding current there is
    # NO copper loss, so the deceleration is driven ONLY by iron (hysteresis+eddy) + windage + friction.
    # Fast coast => that non-copper drag is where the driven current goes (mechanical/iron limited).
    # Slow coast => drag is tiny, so the driven amps are going to copper I2R / electrical (R-limited). ---
    node.sdo['TargetVelocity'].raw = 0
    node.sdo['ControlWord'].raw = 0x00              # DS402 "Disable Voltage" -> Switch-On-Disabled: opens
                                                    # the bridge (HIGH-Z) so the rotor truly freewheels.
                                                    # (SHUTDOWN/0x06 shorts the phases here = dynamic brake.)
    cT, cRPM = [], []
    c0 = time.time()
    coast_start = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
    while True:
        now = time.time() - c0
        rpm = abs(node.sdo['VelocityFeedback'].raw) * 60.0 / enc_res
        cT.append(now); cRPM.append(rpm)
        if coast_start > 0 and rpm <= COAST_FLOOR * coast_start:
            break
        if now >= COAST_TIMEOUT:
            break
    temp1 = _motor_temp()

    # --- analyse ---
    def stats(t_lo, t_hi):
        rr = _win(T, RPM, t_lo, t_hi); qq = _win(T, IQ, t_lo, t_hi)
        dd = _win(T, ID, t_lo, t_hi); vv = _win(T, VF, t_lo, t_hi)
        bb = _win(T, BUS, t_lo, t_hi); ii = _win(T, I2, t_lo, t_hi)
        return {'rpm': _mean(rr), 'rpm_sd': _std(rr), 'iq_m': _mean(qq), 'iq_sd': _std(qq),
                'id_m': _mean(dd), 'v': _mean(vv), 'bus': _mean(bb), 'i2': _mean(ii)}

    # PEAK = the fastest the motor actually reached (max RPM), not a current-threshold guess.
    rpm_max = max(RPM)
    im = RPM.index(rpm_max)
    t_max = T[im]
    peak = stats(max(0.0, t_max - 0.5), t_max)               # approach to the max (current still high)
    peak['rpm'] = rpm_max
    cont = stats(T[-1] - WIN_S, T[-1])                       # tail = settled / post-fold
    # peak modulation can spike past its window-mean (voltage grazing on the swings) -- track the max
    # over the whole high-current phase (before the current folds toward i_cont).
    v_pre = [VF[i] for i in range(len(T)) if IQ[i] > 1.5 * max(i_cont, 1)]
    v_max = max(v_pre) if v_pre else max(VF)
    # were we still climbing on the run-up to the peak? (slope just before the max)
    ts = [T[i] for i in range(len(T)) if t_max - 1.0 <= T[i] <= max(0.0, t_max - 0.2)]
    ys = [RPM[i] for i in range(len(T)) if t_max - 1.0 <= T[i] <= max(0.0, t_max - 0.2)]
    climb = _slope(ts, ys)                                    # RPM/s approaching the peak

    # coast-down: freewheel deceleration (RPM/s) high-speed vs low-speed -> drag speed-dependence
    coast_ok = len(cT) >= 4 and coast_start > 0 and cRPM[-1] < 0.9 * coast_start

    def _coast_decel(lo_frac, hi_frac):
        lo = lo_frac * coast_start; hi = hi_frac * coast_start
        pts = [(cT[k], cRPM[k]) for k in range(len(cT)) if lo <= cRPM[k] <= hi]
        if len(pts) < 3:
            return 0.0
        return -_slope([p[0] for p in pts], [p[1] for p in pts])
    decel_hi = _coast_decel(0.60, 0.98)                      # near the top of the coast
    decel_lo = _coast_decel(0.20, 0.55)                      # near the bottom
    t_half = next((cT[k] for k in range(len(cT)) if cRPM[k] <= 0.5 * coast_start),
                  cT[-1] if cT else 0.0)

    # derived: back-EMF share (unit-free from the puck's own NoLoadSpeed), elec freq, temp rise, mod clip
    e_pct_peak = 100.0 * peak['rpm'] / no_load if no_load > 0 else 0.0
    e_pct_cont = 100.0 * cont['rpm'] / no_load if no_load > 0 else 0.0
    e_v_peak = cont['bus'] * peak['rpm'] / no_load if no_load > 0 else 0.0
    e_hz_peak = peak['rpm'] / 60.0 * pole_pairs
    clip_frac = 100.0 * sum(1 for v in VF if v >= 100.0) / len(VF) if VF else 0.0
    d_temp = (temp1 - temp0) if (temp0 is not None and temp1 is not None) else None
    # driven drag (coast-immune): at the continuous steady state torque = drag, and torque = ke*iq with
    # ke = bus / omega_no_load (V.s/rad). Gives the total NON-copper mechanical loss the current fights,
    # without needing a freewheel (this firmware brakes in every disabled state, so a real coast is out).
    ke_si   = (cont['bus'] / (no_load * math.pi / 30.0)) if no_load > 0 else 0.0   # N.m/A
    drag_nm = ke_si * (cont['iq_m'] / 1000.0)                                      # N.m at continuous
    drag_w  = drag_nm * (cont['rpm'] * math.pi / 30.0)                             # W at continuous

    # tee every report line to a buffer so we can write the same block to a log
    out = []

    def emit(s=""):
        print(s)
        out.append(s)

    drop = peak['rpm'] - cont['rpm']
    drop_pct = 100.0 * drop / peak['rpm'] if peak['rpm'] > 0 else 0.0
    folded = drop_pct > 3.0 and cont['i2'] > peak['i2'] and peak['iq_m'] > 1.15 * max(cont['iq_m'], 1)

    # --- header: model + LagFactor make each run self-identifying for baseline-vs-cal compare ---
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    emit("=" * 66)
    emit("TOP-SPEED VITALS   {}   PWM={}kHz   LagFactor={}   Settle={}ns   {}".format(
        model, pwm_hz // 1000 if pwm_hz else "?", lag if lag >= 0 else "?",
        set_ns if set_ns >= 0 else "?", stamp))
    emit("=" * 66)
    if i2t_start > I2T_READY:
        emit("  NOTE: started with i2t={} (still hot after {:.0f}s) -- PEAK MAY BE TRUNCATED.".format(
            i2t_start, i2t_waited))
    elif i2t_waited > 0:
        emit("  (waited {:.0f}s for i2t to bleed to {} before spin-up)".format(i2t_waited, i2t_start))
    # --- timeline digest so the spin-up -> peak -> fold shape is visible ---
    emit("TIMELINE   ({} samples, {:.1f} s)".format(len(T), T[-1]))
    emit("   t(s)     RPM   iq(mA)  i2t   %mod")
    step = max(1, len(T) // 16)
    for i in range(0, len(T), step):
        mark = "  <- peak" if i <= im < i + step else ""
        emit("  {:5.2f}  {:6.0f}   {:6.0f} {:5.0f}   {:3.0f}{}".format(
            T[i], RPM[i], IQ[i], I2[i], VF[i], mark))

    def row(label, p, c, fmt="{:.0f}"):
        return "  {:<21}{:>15}{:>17}".format(label, fmt.format(p), fmt.format(c))
    emit("-" * 66)
    emit("  {:<21}{:>15}{:>17}".format("", "PEAK (max RPM)", "CONTINUOUS"))
    emit(row("RPM:", peak['rpm'], cont['rpm']))
    emit(row("RPM swing (std):", peak['rpm_sd'], cont['rpm_sd']))
    emit(row("iq mean (mA):", peak['iq_m'], cont['iq_m']))
    emit(row("iq swing (std):", peak['iq_sd'], cont['iq_sd']))
    emit(row("id mean (mA):", peak['id_m'], cont['id_m']))
    emit(row("applied |V| (%mod):", peak['v'], cont['v']))
    emit("  peak |V| max (pre-fold): {:.0f}%  (>=98% = grazing the voltage ceiling)".format(v_max))
    emit(row("bus (V):", peak['bus'], cont['bus'], "{:.1f}"))
    emit(row("i2t accumulator:", peak['i2'], cont['i2']))
    emit("  peak reached at t={:.1f}s; RPM slope on the run-up: {:+.0f} RPM/s".format(t_max, climb))
    emit("  i_cont: {} mA   fold-target (TempLimitedContinuousCurrent): {} mA".format(i_cont, i_templim))
    emit("-" * 66)
    emit("WHERE THE SPEED IS GOING:")
    emit("  MOTOR (OD): poles {}  kt {}  rt {}  lt {}  j {}  NoLoadSpeed {} RPM".format(
        poles, kt_raw, rt_raw, lt_raw, j_raw, no_load))
    if no_load > 0:
        emit("  % of no-load spec: PEAK {:.0f} = {:.0f}%   CONTINUOUS {:.0f} = {:.0f}%   of {} RPM".format(
            peak['rpm'], e_pct_peak, cont['rpm'], e_pct_cont, no_load))
        emit("  back-EMF @peak = bus x rpm/no_load ~= {:.1f} V = {:.0f}% of bus, but applied ~{:.0f}% mod"
             .format(e_v_peak, e_pct_peak, peak['v']))
        emit("     -> back-EMF uses only {:.0f}% of the bus; the ~{:.0f}% remainder is IR+reactive from the"
             " current draw,".format(e_pct_peak, max(0.0, peak['v'] - e_pct_peak)))
        emit("        NOT back-EMF. You are RESISTANCE/current-limited here, not back-EMF/FW-limited.")
    if pole_pairs:
        emit("  elec freq @peak: {:.0f} Hz ({} pole-pairs) -- higher => bigger iron/eddy loss".format(
            e_hz_peak, pole_pairs))
    if d_temp is not None:
        emit("  motor temp: {} -> {} C  (rise {:+d} over the run = where the lost power turned to heat)"
             .format(temp0, temp1, d_temp))
    emit("  modulation clipping: {:.0f}% of samples at >=100% mod".format(clip_frac))
    if no_load > 0 and cont['rpm'] > 0:
        emit("  driven drag @continuous: ~{:.1f} mNm = ~{:.1f} W at {:.0f} RPM  (ke*iq, steady-state"
             .format(drag_nm * 1000.0, drag_w, cont['rpm']))
        emit("     torque=drag -> total NON-copper loss the current fights; coast-immune)")
    emit("-" * 66)
    if folded:
        emit("  => i2t FOLDED YOU DOWN: current {:.0f}->{:.0f} mA cost {:.0f} RPM ({:.0f}%) from peak"
             " to continuous.".format(peak['iq_m'], cont['iq_m'], drop, drop_pct))
        if v_max < 98.0:
            emit("     Peak is CURRENT-limited (|V| max only {:.0f}%): more current lifts the PEAK too,"
                 " and raising".format(v_max))
            emit("     the i2t limit (i_cont {} / fold-target {} mA, thermal headroom permitting) lifts"
                 " the CONTINUOUS.".format(i_cont, i_templim))
        else:
            emit("     Peak already GRAZES the voltage ceiling (|V| max {:.0f}%): raising the i2t limit"
                 " recovers the".format(v_max))
            emit("     continuous toward the peak, but pushing the PEAK higher needs FW/duty/bus, not"
                 " more current.")
    else:
        if drop_pct <= 3.0:
            emit("  => PEAK ~= CONTINUOUS ({:.0f} RPM, {:.0f}%): i2t is NOT folding you down here."
                 .format(drop, drop_pct))
        else:
            emit("  => RPM dropped {:.0f} ({:.0f}%) but not via an i2t current fold (iq/i2t don't match a"
                 " fold).".format(drop, drop_pct))
        if peak['v'] >= 85.0:
            emit("     |V| at peak = {:.0f}% -> VOLTAGE-limited: peak lives on FW/duty/bus, not current."
                 .format(peak['v']))
        else:
            emit("     |V| at peak only {:.0f}% and iq only {:.0f} mA -> neither volt- nor i2t-pinned;"
                 " likely a real".format(peak['v'], peak['iq_m']))
            emit("     load/windage ceiling (or the run ended before the fold -- check the timeline tail).")
    emit("-" * 66)
    if coast_ok and t_half < 0.6 and coast_start > 1500:
        emit("COAST-DOWN: INVALID -- decayed {:.0f}->50% in {:.2f}s. That's far too fast for a freewheel"
             " (a smooth".format(coast_start, t_half))
        emit("  rotor coasts for SECONDS), so the bridge is still DYNAMIC-BRAKING in this disabled state,")
        emit("  not high-Z. Can't decompose iron/windage/friction -- use the driven-drag figure above.")
    elif coast_ok:
        emit("COAST-DOWN (drive OFF / freewheel -- isolates iron+windage+friction, ZERO copper loss):")
        emit("  start {:.0f} RPM;  time to 50%: {:.1f}s;  decel HIGH-speed {:.0f} vs LOW-speed {:.0f} RPM/s"
             .format(coast_start, t_half, decel_hi, decel_lo))
        if decel_hi > 1.6 * max(decel_lo, 1.0):
            emit("  => drag RISES with speed (iron/eddy + windage): real non-copper loss at the top is")
            emit("     eating into your ceiling -- not just IR. High elec-freq iron loss is the suspect.")
        else:
            emit("  => drag ~FLAT with speed (bearing/friction): non-copper loss is small, so the driven")
            emit("     amps are going to COPPER I2R / electrical -> the ceiling is winding R, not iron/windage.")
        if j_raw > 0:
            emit("  (OD j={} lets you scale decel->drag torque; cross-check kt*iq at continuous vs j*dw/dt)"
                 .format(j_raw))
    else:
        emit("COAST-DOWN: not captured cleanly (velocity didn't decay -- drive may brake in SHUTDOWN, or")
        emit("  the encoder feedback froze). See the raw coast samples appended below.")
    emit("=" * 66)
    if cT:
        emit("COAST SAMPLES  (t(s), RPM):")
        cstep = max(1, len(cT) // 16)
        for k in range(0, len(cT), cstep):
            emit("  {:5.2f}  {:6.0f}".format(cT[k], cRPM[k]))
        emit("=" * 66)

    # --- write the same block to scripts/velocitytests/<model>_lag<lag>_<timestamp>.log ---
    logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'velocitytests')
    os.makedirs(logdir, exist_ok=True)
    fname = "{}_lag{}_set{}_{}.log".format(
        model, lag if lag >= 0 else "NA", set_ns if set_ns >= 0 else "NA",
        time.strftime("%Y%m%d-%H%M%S"))
    fpath = os.path.join(logdir, fname)
    with open(fpath, 'w') as fh:
        fh.write("\n".join(out) + "\n")
    print("\nSaved: {}".format(fpath))


def main():
    net = can_backend.make_network(CAN, bitrate=1_000_000)
    node = None
    orig_settle = None
    try:
        node = net.add_node(NODE, EDS)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        if SETTLING is not None:
            try:
                node.sdo['SetModeOfOperation'].raw = MODE_IDLE       # MaxSettlingTime applies live while idle
                orig_settle = node.sdo['Amp']['MaxSettlingTime'].raw
                node.sdo['Amp']['MaxSettlingTime'].raw = int(SETTLING)
                _rb = node.sdo['Amp']['MaxSettlingTime'].raw
                print("MaxSettlingTime override: {} ns applied (was {} ns, readback {} ns) -- restored on exit"
                      .format(SETTLING, orig_settle, _rb))
            except Exception as _se:
                print("Could not set MaxSettlingTime: {}".format(_se))
        vitals(node)
        print("\nDone. Paste this block back.")
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['TargetVelocity'].raw = 0
                node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                if orig_settle is not None:
                    node.sdo['Amp']['MaxSettlingTime'].raw = orig_settle
                    print("MaxSettlingTime restored to {} ns.".format(orig_settle))
                print("Puck idled.")
        except Exception:
            pass
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
