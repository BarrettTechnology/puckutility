#!/usr/bin/env python3
"""A/B test one actuator on firmware 4.3.5 and 5.0.5: prepare it, run the 50 Hz
CSP trajectories, plot and compare.

    ab-testing.py --can can0 --id 4 prepare --fw 4.3.5 --actuator BO4523
    ab-testing.py --can can0 --id 4 run --actuator BO4523 [--profile chirp,square]
    ab-testing.py plot runs/RUN_A [runs/RUN_B]
    ab-testing.py compare runs/RUN_A runs/RUN_B
    Windows: --can 0 is the first CandleLight (or PCAN) adapter.

prepare = nv save -> flash -> wipe -> load the actuator's CSV -> the 4
calibration steps (bias, gain, encoder zero + winding order) -> read every CSV
row back -> nv save.  Each stage is also a subcommand of its own (flash, wipe,
load, calibrate); they call the p4core CLI, as the guide does.  Never enable a
drive that was downgraded to 4.3.5 before wipe, load and calibrate have run:
5.x keeps the current-sense bias as Q12.4 and 4.3.5 reads it as whole counts.
run refuses a drive whose bias or gain factor is not plausible for its firmware.

run drives cyclic sync position (CSP, mode 8) with a 50 Hz SYNC from the host
and 0x60C2 = 20 ms.  Every profile is defined in output-shaft degrees, so the
same profile is the same motion on both firmwares.  The PDOs are mapped here,
in PRE-OPERATIONAL, and put back afterwards (RAM only):
  RPDO1 = 0x6040 controlword, 0x607A target position             (SYNC, 1)
  TPDO1 = 0x3008,1 raw alpha, 0x3009,1 raw beta, 0x60EA theta_e,
          0x6078 current                                          (SYNC, 1)
  TPDO2 = 0x6064 position, 0x606C velocity                        (SYNC, 1)
  TPDO3 = 0x60F4 following error, 0x3010,9 iq, 0x3010,12 id, mA   (5.x only)
4.3.5 sends TPDO1 and TPDO2 only, so TPDO1/2 are the same-path data on both.
Each run writes runs/<date>-<time>-n<id>-<fw>-<profile>/ with meta.json and
data.npz.  plot draws Id/Iq, velocity error and position error; compare prints
the metrics of two runs side by side and writes compare.json.
"""

import argparse
import datetime
import glob
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(os.path.dirname(HERE), 'sandbox'), os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                          # noqa: E402

RES = 4096                  # encoder counts per motor revolution (MA736, both CSVs)
RATE_HZ = 50.0              # the SYNC and set-point rate of every run
COB_INVALID = 0x80000000

#: The actuators.  ratio = 0x6091 motor/shaft revolutions (checked on the
#: bench 2026-11-06); vmax and accel (cts/s, cts/s^2) are 0x6080 and the lower
#: 0x6083 of the actuator's two CSVs, so both firmwares get the same motion.
ACTUATORS = {
    'BO4523': {'board': 'P4-42', 'ratio': (264, 10), 'vmax': 682667, 'accel': 908800,
               'csv': 'P4-42-*-BO4523-*fw{fw}-v*.csv'},
    'BO3518': {'board': 'P4-32', 'ratio': (60840, 1000), 'vmax': 682667, 'accel': 1022803,
               'csv': 'P4-32-*-BO3518-*fw{fw}-v*.csv'},
}
FIRMWARE = ('4.3.5', '5.0.5')

#: The trajectories, in output degrees and seconds.  Each runs `reps` times;
#: the second half of each repetition is the first mirrored, so both
#: directions are covered.
PROFILES = {
    'chirp': {'kind': 'chirp', 'f0': 0.2, 'f1': 10.0, 'seconds': 20.0, 'vel': 90.0,
              'help': 'position sine swept 0.2 -> 10 Hz (log), constant velocity '
                      'amplitude until the acceleration limit'},
    'square': {'kind': 'square', 'amps': (0.5, 5.0), 'hold': 1.0,
               'help': 'position steps 0, +A, 0, -A at 0.5 and 5 output degrees, 1 s each'},
    'ramp': {'kind': 'ramp', 'speeds': (10.0, 90.0), 'seconds': 3.0,
             'help': 'constant velocity at 10 and 90 output deg/s, each way, for ripple'},
    'hold': {'kind': 'hold', 'seconds': 5.0,
             'help': 'set-point held still, for the dither at rest'},
}
REPS = 2

PDO_MAP = {
    # name: (comm, mapping, cob base, transmission, ((index, sub, bits), ...))
    # RPDO1 is synchronous: the target applies at the SYNC after it, which is when the
    # drive takes the previous one as the start of the interpolation.  Asynchronous
    # (255), it lands before that SYNC and the drive jumps to it with no feed-forward.
    'rpdo1': (0x1400, 0x1600, 0x200, 1, ((0x6040, 0, 16), (0x607A, 0, 32))),
    'tpdo1': (0x1800, 0x1A00, 0x180, 1, ((0x3008, 1, 16), (0x3009, 1, 16),
                                         (0x60EA, 0, 16), (0x6078, 0, 16))),
    'tpdo2': (0x1801, 0x1A01, 0x280, 1, ((0x6064, 0, 32), (0x606C, 0, 32))),
    'tpdo3': (0x1802, 0x1A02, 0x380, 1, ((0x60F4, 0, 32), (0x3010, 9, 16),
                                         (0x3010, 12, 16))),
}
TPDO_FMT = {'tpdo1': '<HHhh', 'tpdo2': '<ii', 'tpdo3': '<ihh'}
TPDO_FIELDS = {'tpdo1': ('raw_a', 'raw_b', 'theta', 'i6078'),
               'tpdo2': ('pos', 'vel'),
               'tpdo3': ('err60f4', 'iq_fw', 'id_fw')}


class AbError(Exception):
    """A stage or a run could not go on."""


# ---------------------------------------------------------------------------
# Units and trajectories
# ---------------------------------------------------------------------------
def cts_per_deg(actuator):
    m, s = ACTUATORS[actuator]['ratio']
    return RES * m / s / 360.0


def output_limits(actuator):
    """(speed, acceleration) limits in output deg/s and deg/s^2: half of
    0x6080 and all of 0x6083."""
    a = ACTUATORS[actuator]
    k = cts_per_deg(actuator)
    return 0.5 * a['vmax'] / k, a['accel'] / k


def _taper(t, total, rise):
    """Raised-cosine fade in over *rise* s and out over the last *rise* s."""
    w = np.ones_like(t)
    up = t < rise
    w[up] = 0.5 - 0.5 * np.cos(math.pi * t[up] / rise)
    down = t > total - rise
    w[down] = 0.5 - 0.5 * np.cos(math.pi * (total - t[down]) / rise)
    return w


def _chirp(p, vmax, amax, rate):
    T = p['seconds']
    t = np.arange(int(round(T * rate)) + 1) / rate
    k = math.log(p['f1'] / p['f0']) / T
    f = p['f0'] * np.exp(k * t)
    phase = 2 * math.pi * p['f0'] * (np.exp(k * t) - 1) / k
    w = 2 * math.pi * f
    vel = min(p['vel'], vmax)
    amp = np.minimum(vel / w, amax / w ** 2)
    return amp * np.sin(phase) * _taper(t, T, min(2.0, T / 4))


def _square(p, rate):
    n = int(round(p['hold'] * rate))
    out = []
    for a in p['amps']:
        for level in (0.0, a, 0.0, -a):
            out += [level] * n
    out += [0.0] * n
    return np.array(out)


def _ramp(p, vmax, amax, rate):
    """Trapezoids: up to each speed, hold it for `seconds`, back to 0; then the
    same the other way.  Position integrates the velocity, so it ends where
    the trapezoids put it."""
    dt = 1.0 / rate
    vel = []
    for v in p['speeds']:
        v = min(v, vmax)
        for sign in (1, -1):
            n_acc = max(1, int(math.ceil(v / amax * rate)))
            up = np.linspace(0, v, n_acc + 1)[1:]
            vel += list(sign * up) + [sign * v] * int(round(p['seconds'] * rate))
            vel += list(sign * up[::-1][1:]) + [0.0] * int(round(0.5 * rate))
    return np.concatenate([[0.0], np.cumsum(vel) * dt])


def trajectory(profile, actuator, rate=RATE_HZ, reps=REPS):
    """Set-points (output degrees, relative to the start) at *rate*, and the
    repetition index of each.  The second half of a repetition mirrors the
    first: chirp and hold are run negated, square and ramp already go both
    ways."""
    p = PROFILES[profile]
    vmax, amax = output_limits(actuator)
    if p['kind'] == 'chirp':
        one = _chirp(p, vmax, amax, rate)
        one = np.concatenate([one, -one[1:]])
    elif p['kind'] == 'square':
        one = _square(p, rate)
    elif p['kind'] == 'ramp':
        one = _ramp(p, vmax, amax, rate)
    elif p['kind'] == 'hold':
        one = np.zeros(int(round(p['seconds'] * rate)))
    else:
        raise ValueError(p['kind'])
    xs, rep = [], []
    offset = 0.0
    for r in range(reps):
        xs.append(one + offset)
        rep.append(np.full(len(one), r))
        offset = xs[-1][-1]
    return np.concatenate(xs), np.concatenate(rep)


# ---------------------------------------------------------------------------
# Analysis: alpha/beta -> dq, tracking errors, metrics
# ---------------------------------------------------------------------------
def ma_per_count(gain, shunt):
    """pwm.c: 3.3 V / 4096 counts through shunt (mOhm) x gain (x1000), in mA."""
    return 3.3 / 4096 * 1000.0 / shunt * 1000.0 / gain * 1000.0


def alpha_beta_ma(raw_a, raw_b, cal):
    """The firmware's alpha/beta in mA from the raw ADC counts:
    (bias - raw) x gainfactor / 4096 x mA per count (current INTO the phases).
    *cal* holds bias_a/b in counts (5.x's Q12.4 already divided by 16),
    gf_a/b (Q4.12), gain and shunt (0x3008,4-5, used for both channels as
    the firmware does).  5.x's 0x3009,1 is already scaled by the beta
    reference correction (0x3009,9), so nothing is added for it here."""
    k = ma_per_count(cal['gain'], cal['shunt'])
    ia = (cal['bias_a'] - np.asarray(raw_a, float)) * cal['gf_a'] / 4096.0 * k
    ib = (cal['bias_b'] - np.asarray(raw_b, float)) * cal['gf_b'] / 4096.0 * k
    return ia, ib


def park(ia, ib, theta):
    """d = a cos + b sin, q = -a sin + b cos, theta in 0x60EA counts
    (65536 = one electrical revolution)."""
    th = np.asarray(theta, float) * (math.pi / 32768.0)
    c, s = np.cos(th), np.sin(th)
    return ia * c + ib * s, -ia * s + ib * c


def _finite(*arrays):
    m = np.ones(len(arrays[0]), bool)
    for a in arrays:
        m &= np.isfinite(a)
    return m


def analyse(data, meta):
    """Derived series of one run, in output degrees, deg/s and mA.

    The drive takes set-point k at SYNC k and moves to it by SYNC k+1 (linear
    interpolation over the SYNC period), so at SYNC k its reference is
    set-point k-1, and its reference velocity there the mean of the slopes
    either side: (sp[k] - sp[k-2]) / 2T.  The TPDOs are sampled at SYNC k."""
    T = 1.0 / meta['rate_hz']
    kdeg = meta['cts_per_deg']
    sp = np.asarray(data['sp'], float)
    ref = np.full_like(sp, np.nan)
    ref[1:] = sp[:-1]
    vref = np.full_like(sp, np.nan)
    vref[2:] = (sp[2:] - sp[:-2]) / (2 * T)
    pos = np.asarray(data['pos'], float)
    vel = np.asarray(data['vel'], float)
    vhost = np.full_like(pos, np.nan)
    vhost[1:-1] = (pos[2:] - pos[:-2]) / (2 * T)
    ia, ib = alpha_beta_ma(data['raw_a'], data['raw_b'], meta['cal'])
    idq, iqq = park(ia, ib, data['theta'])
    # The user's sign of torque.  The Park frame follows the windings, so its
    # sign differs between pucks.  On 5.x the drive's own iq (0x3010,9) gives
    # it.  On 4.3.5: over a motion that comes back, the drive's net work goes
    # into friction, so mean iq x velocity > 0 fixes the sign.  That fails on
    # 5.x square steps, which settle within a SYNC or two: sampled at 50 Hz
    # the braking outweighs (bench, 2026-11-06).  0x6078 is only a check --
    # on 4.3.5 it follows steps loosely and has given either sign on the same
    # node.  Without motion or drive iq (4.3.5 hold) use +1, what every
    # moving bench run gave on nodes 4 and 13, both lines (2026-11-06).
    m6 = _finite(iqq, data['i6078'])
    corr6 = float('nan')
    if m6.sum() > 10 and np.std(iqq[m6]) > 0 and np.std(data['i6078'][m6]) > 0:
        corr6 = float(np.corrcoef(iqq[m6], data['i6078'][m6])[0, 1])
    corr_fw = float('nan')
    if 'iq_fw' in data:
        mf = _finite(iqq, data['iq_fw'])
        if mf.sum() > 10 and np.std(iqq[mf]) > 0 and np.std(data['iq_fw'][mf]) > 0:
            corr_fw = float(np.corrcoef(iqq[mf], data['iq_fw'][mf])[0, 1])
    m = _finite(iqq, vhost) & (np.abs(np.nan_to_num(vref)) > 0)
    if np.isfinite(corr_fw):
        sign = 1.0 if corr_fw > 0 else -1.0
    elif m.sum() > 10:
        sign = 1.0 if np.sum(iqq[m] * vhost[m]) > 0 else -1.0
    else:
        sign = 1.0
    corr = sign * corr6
    out = {
        't': np.arange(len(sp)) * T, 'rate_hz': meta['rate_hz'], 'sp_deg': sp / kdeg,
        'ref_deg': ref / kdeg, 'pos_deg': pos / kdeg,
        'pos_err_deg': (ref - pos) / kdeg,
        'vref_dps': vref / kdeg,
        # against d(0x6064)/dt: the same measure on both lines.  4.3.5's 0x606C
        # reads ~0.56x the motion in CSP (bench, 2026-11-06), so it is kept apart.
        'vel_err_dps': (vref - vhost) / kdeg,
        'vel_err_606c_dps': (vref - vel) / kdeg,
        'id_ma': idq, 'iq_ma': sign * iqq,
        'iq_sign': sign, 'iq_6078_corr': corr,
    }
    if 'err60f4' in data and np.isfinite(data['err60f4']).any():
        out['err60f4_deg'] = np.asarray(data['err60f4'], float) / kdeg
    if 'iq_fw' in data and np.isfinite(data['iq_fw']).any():
        out['iq_fw_corr'] = sign * corr_fw
    return out


def _rms(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else float('nan')


def _peak(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.max(np.abs(x))) if len(x) else float('nan')


def fit_lag(sp, pos, rate, max_periods=5.0, step=0.05):
    """(lag in s, rms error in counts) of the delay of *pos* behind the
    set-points *sp* (both sampled at SYNC k) that fits best.  The drive's
    reference is sp[k-1], a lag of one period; a longer one is the drive's."""
    sp = np.asarray(sp, float)
    pos = np.asarray(pos, float)
    n = np.arange(len(sp))
    best = (float('nan'), float('nan'))
    for lag in np.arange(0.0, max_periods + step / 2, step):
        ref = np.interp(n - lag, n, sp)
        e = (ref - pos)[int(math.ceil(max_periods)):]
        e = e[np.isfinite(e)]
        if len(e):
            r = float(np.sqrt(np.mean(e ** 2)))
            if not r >= best[1]:
                best = (lag / rate, r)
    return best


def metrics(a):
    """Summary numbers of one analysed run."""
    moving = np.abs(np.nan_to_num(a['vref_dps'])) > 1e-6
    still = ~moving
    out = {
        'pos_err_rms_deg': _rms(a['pos_err_deg']),
        'pos_err_peak_deg': _peak(a['pos_err_deg']),
        'vel_err_rms_dps': _rms(a['vel_err_dps']),
        'vel_err_606c_rms_dps': _rms(a['vel_err_606c_dps']),
        'iq_rms_ma': _rms(a['iq_ma']),
        'iq_peak_ma': _peak(a['iq_ma']),
        'id_rms_ma': _rms(a['id_ma']),
        'id_peak_ma': _peak(a['id_ma']),
        'iq_6078_corr': a['iq_6078_corr'],
    }
    if still.sum() > 10:
        p = np.asarray(a['pos_deg'])[still]
        out['still_pos_sd_deg'] = float(np.nanstd(p - np.asarray(a['ref_deg'])[still]))
        out['still_iq_rms_ma'] = _rms(np.asarray(a['iq_ma'])[still])
    if moving.sum() > 10:
        out['moving_iq_sd_ma'] = float(np.nanstd(np.asarray(a['iq_ma'])[moving]))
    if moving.sum() > 10:
        lag, rms = fit_lag(a['sp_deg'], a['pos_deg'], a['rate_hz'])
        out['lag_ms'] = 1000.0 * lag
        out['pos_err_at_lag_rms_deg'] = rms
    if 'err60f4_deg' in a:
        out['err60f4_rms_deg'] = _rms(a['err60f4_deg'])
    if 'iq_fw_corr' in a:
        out['iq_fw_corr'] = a['iq_fw_corr']
    return out


# ---------------------------------------------------------------------------
# Run directories
# ---------------------------------------------------------------------------
def run_dir(root, node_id, fw, profile, when=None):
    when = when or datetime.datetime.now()
    return os.path.join(root, '{}-n{}-{}-{}'.format(
        when.strftime('%Y%m%d-%H%M%S'), node_id, fw, profile))


def save_run(path, meta, data):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2, sort_keys=True, default=_json_default)
    np.savez(os.path.join(path, 'data.npz'), **{k: np.asarray(v) for k, v in data.items()})


def load_run(path):
    with open(os.path.join(path, 'meta.json')) as f:
        meta = json.load(f)
    with np.load(os.path.join(path, 'data.npz')) as z:
        data = {k: z[k] for k in z.files}
    return meta, data


def _json_default(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    raise TypeError(type(x))


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Finding the CSV and firmware of an actuator
# ---------------------------------------------------------------------------
def _version_key(path):
    m = re.search(r'-v(\d+)\.(\d+)\.(\d+)', os.path.basename(path))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_csv(actuator, fw, csv_dir):
    pattern = ACTUATORS[actuator]['csv'].format(fw=fw)
    found = sorted(glob.glob(os.path.join(csv_dir, pattern)), key=_version_key)
    if not found:
        raise AbError('no CSV for {} on {} in {} ({})'.format(actuator, fw, csv_dir, pattern))
    return found[-1]


def find_firmware(fw, fw_dir):
    # The .bin first: flashloader 3.0.7 rejects the CI .ebin (another key).
    found = sorted(glob.glob(os.path.join(fw_dir, '*{}*.bin'.format(fw)))) + \
        sorted(glob.glob(os.path.join(fw_dir, '*{}*.ebin'.format(fw))))
    if not found:
        raise AbError('no {} firmware (.ebin or .bin) in {}: give --bin'.format(fw, fw_dir))
    return found[0]


def fw_line(version):
    """'4.3.5' for a 4.x version, '5.0.5' for 5.x (the bench's 5.0.5 candidate
    still reports 5.0.4)."""
    return '4.3.5' if str(version).startswith('4.') else '5.0.5'


# ---------------------------------------------------------------------------
# The stages: the p4core CLI, as the guide documents it
# ---------------------------------------------------------------------------
class Stages:
    """Runs `python -m p4core --can C --id N -y ...` and logs its output."""

    def __init__(self, can, node_id, log_path=None, out=print, runner=subprocess.run):
        self.can = can
        self.node_id = node_id
        self.log_path = log_path
        self.out = out
        self.runner = runner

    def p4core(self, *args, timeout=900):
        cmd = [sys.executable, '-m', 'p4core', '--can', str(self.can), '--id',
               str(self.node_id), '-y'] + [str(a) for a in args]
        self.out('$ p4core ' + ' '.join(str(a) for a in args))
        r = self.runner(cmd, capture_output=True, text=True, timeout=timeout)
        text = (r.stdout or '') + (r.stderr or '')
        lines = [ln for ln in text.splitlines() if not re.fullmatch(r'\s*\d+%', ln)]
        for ln in lines[-12:]:
            self.out('  ' + ln)
        if self.log_path:
            with open(self.log_path, 'a') as f:
                f.write('$ ' + ' '.join(cmd) + '\n' + '\n'.join(lines) + '\n'
                        + 'exit {}\n\n'.format(r.returncode))
        if r.returncode != 0:
            raise AbError('p4core {} failed (exit {})'.format(args[0], r.returncode))
        return '\n'.join(lines)

    def version(self):
        text = self.p4core('version', timeout=60)
        last = text.strip().splitlines()[-1].split()
        return last[0], (last[1] if len(last) > 1 else 'n/a')

    def nv_save(self, path):
        self.p4core('nv', 'save', path)

    def flash(self, path):
        self.p4core('flash', path, timeout=900)

    def wipe(self, backup):
        self.p4core('wipe-nvm', '--no-template', '--backup', backup)

    def load(self, csv):
        self.p4core('config', 'load', csv)

    def calibrate(self, steps=('ab',)):
        self.p4core('calibrate', *steps)


def verify_csv(can, node_id, csv, out=print):
    """Every CSV row read back from the puck by SDO (csvdiff)."""
    import csvdiff
    rc = csvdiff.main([csv, '--can', str(can), '--id', str(node_id)])
    if rc:
        raise AbError('the puck does not hold {} (see the rows above)'.format(
            os.path.basename(csv)))


def prepare(args, out=print):
    act = args.actuator
    csv = args.csv or find_csv(act, args.fw, args.csv_dir)
    fwfile = args.bin or find_firmware(args.fw, args.firmware_dir)
    pdir = run_dir(args.runs, args.id, args.fw, 'prepare')
    os.makedirs(pdir, exist_ok=True)
    st = Stages(args.can, args.id, os.path.join(pdir, 'prepare.log'), out)
    out('prepare node {}: {} on {} with {}'.format(args.id, act, os.path.basename(fwfile),
                                                   os.path.basename(csv)))
    st.nv_save(os.path.join(pdir, 'nv_before.json'))
    if not args.no_flash:
        st.flash(fwfile)
    version, githash = st.version()
    if fw_line(version) != args.fw:
        raise AbError('node {} runs {} after the flash, not {}'.format(args.id, version, args.fw))
    st.wipe(os.path.join(pdir, 'prewipe.csv'))
    st.load(csv)
    st.calibrate()
    verify_csv(args.can, args.id, csv, out)
    st.nv_save(os.path.join(pdir, 'nv_after.json'))
    meta = {'node': args.id, 'actuator': act, 'fw': args.fw, 'version': version,
            'githash': githash, 'firmware_file': os.path.basename(fwfile),
            'firmware_sha256': file_sha256(fwfile), 'csv': os.path.basename(csv),
            'csv_sha256': file_sha256(csv),
            'date': datetime.datetime.now().isoformat(timespec='seconds')}
    with open(os.path.join(pdir, 'prepare.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    out('prepared: {} (record in {})'.format(version, pdir))
    return pdir


# ---------------------------------------------------------------------------
# The run: CSP at 50 Hz with the PDOs above
# ---------------------------------------------------------------------------
def _drive_module():
    import testp4
    return testp4


def read_cal(d):
    """The current-sense calibration the firmware converts with, in counts."""
    q = 16.0 if not d.legacy else 1.0
    return {'bias_a': d.r(0x3008, 3, 'H') / q, 'bias_b': d.r(0x3009, 3, 'H') / q,
            'gf_a': d.r(0x3008, 6, 'H'), 'gf_b': d.r(0x3009, 6, 'H'),
            'gain': d.r(0x3008, 4, 'H'), 'shunt': d.r(0x3008, 5, 'H')}


def check_calibrated(cal, legacy):
    """AbError unless bias and gain factor look calibrated for this firmware.
    A bias left in 5.x's Q12.4 reads ~32768 on 4.3.5, far outside the window."""
    problems = []
    for ch in 'ab':
        b, g = cal['bias_' + ch], cal['gf_' + ch]
        if not 1500 <= b <= 2600:
            problems.append('bias {} = {:.0f} counts'.format(ch, b))
        if not 2048 <= g <= 8192:
            problems.append('gain factor {} = {}'.format(ch, g))
    if problems:
        raise AbError('the current sense is not calibrated for {} ({}): run prepare, or '
                      'wipe, load and calibrate'.format('4.3.5' if legacy else '5.x',
                                                        ', '.join(problems)))


class Recorder:
    """Collects the TPDOs of each SYNC.  The loop sets `k` just before it
    sends SYNC k, so every frame that arrives after is SYNC k's."""

    def __init__(self, names, n):
        self.k = -1
        self.cols = {}
        for name in names:
            for f in TPDO_FIELDS[name]:
                self.cols[f] = np.full(n, np.nan)
        self.count = {name: 0 for name in names}

    def handler(self, name):
        fmt, fields = TPDO_FMT[name], TPDO_FIELDS[name]
        size = struct.calcsize(fmt)

        def on(cob, data, timestamp=None):
            k = self.k
            data = bytes(data)
            if k < 0 or len(data) < size:
                return
            vals = struct.unpack_from(fmt, data)
            for f, v in zip(fields, vals):
                if k < len(self.cols[f]):
                    self.cols[f][k] = v
            self.count[name] += 1
        return on


def map_run_pdos(d, names):
    """Map *names* of PDO_MAP in PRE-OPERATIONAL, keeping the old mapping in
    d.pdo_saved for d.unmap_pdos().  RPDO1 stays invalid until the loop."""
    d.node.nmt.state = 'PRE-OPERATIONAL'
    d.sleep(0.05)
    for name in names:
        comm, mapping, base, trans, objects = PDO_MAP[name]
        tpdo = name.startswith('t')
        saved, entries, n = d._snapshot_pdo(comm, mapping, (1, 2, 3, 5) if tpdo else (1, 2))
        d.pdo_saved.append((comm, mapping, saved, entries, n))
        cob = base + d.id
        d.w(comm, 1, cob | COB_INVALID, 'I')
        d.w(comm, 2, trans, 'B')
        if tpdo:
            for sub in (3, 5):
                try:
                    d.w(comm, sub, 0, 'H')
                except Exception:       # noqa: BLE001 -- 4.3.5 lacks some subs
                    pass
        d.w(mapping, 0, 0, 'B')
        for k, (index, sub, bits) in enumerate(objects, 1):
            d.w(mapping, k, (index << 16) | (sub << 8) | bits, 'I')
        d.w(mapping, 0, len(objects), 'B')
        if tpdo:
            d.w(comm, 1, cob, 'I')
    d.node.nmt.state = 'OPERATIONAL'
    d.sleep(0.05)


def _rpdo1(d, valid):
    comm, _, base, _, _ = PDO_MAP['rpdo1']
    d.w(comm, 1, (base + d.id) | (0 if valid else COB_INVALID), 'I')


def run_profile(d, actuator, profile, args, out=print):
    """One profile on a calibrated drive: (meta, data)."""
    T = _drive_module()
    rate = RATE_HZ
    kdeg = cts_per_deg(actuator)
    xs, rep = trajectory(profile, actuator, rate, args.reps)
    settle = int(round(args.settle * rate))
    n = settle + len(xs) + settle
    names = ['tpdo1', 'tpdo2'] + ([] if d.legacy else ['tpdo3'])
    rec = Recorder(names, n)
    meta = {'profile': profile, 'profile_def': PROFILES[profile], 'actuator': actuator,
            'node': d.id, 'version': d.version, 'githash': d.githash,
            'fw': '4.3.5' if d.legacy else '5.0.5', 'rate_hz': rate, 'reps': args.reps,
            'cts_per_deg': kdeg, 'ratio': ACTUATORS[actuator]['ratio'],
            'settle_s': args.settle,
            'date': datetime.datetime.now().isoformat(timespec='seconds')}
    meta['cal'] = read_cal(d)
    check_calibrated(meta['cal'], d.legacy)
    m, s = d.r(0x6091, 1, 'I'), d.r(0x6091, 2, 'I')
    if s == 0 or abs(m / s - kdeg * 360 / RES) > 1e-6 * m / s:
        raise AbError('0x6091 = {}/{}, but {} is {}/{}: is this the right CSV?'.format(
            m, s, actuator, *ACTUATORS[actuator]['ratio']))
    meta['bus_v_before'] = d.r(0x3000, 1, 'H')
    meta['temp_before'] = d.r(0x3000, 2, 'h')
    meta['i_peak'] = d.r(0x3011, 8, 'H') if d.has(0x3011, 8) else None
    guard_v = 0.8 * ACTUATORS[actuator]['vmax']
    guard_err = args.max_error_revs * RES
    sp = np.full(n, np.nan)
    sent = np.full(n, np.nan)
    try:
        map_run_pdos(d, ['rpdo1'] + names)
        for name in names:
            d.network.subscribe(PDO_MAP[name][2] + d.id, rec.handler(name))
        T.enter(d, T.cia402.MODE_CYCLIC_SYNC_POS, rate)
        p0 = d.r(0x6064, 0, 'i')
        meta['p0'] = p0
        targets = np.concatenate([np.zeros(settle), xs, np.full(settle, xs[-1])])
        targets = p0 + np.round(targets * kdeg)
        _rpdo1(d, True)
        t0 = d.now()
        period = 1.0 / rate
        last_poll = 0
        for k in range(n):
            tgt = int(targets[k])
            d.network.send_message(0x200 + d.id, struct.pack('<Hi', T.CW_ENABLE, tgt))
            rec.k = k
            d.send_sync()
            sp[k] = tgt
            sent[k] = d.now() - t0
            if k >= 1:
                vel, pos = rec.cols['vel'][k - 1], rec.cols['pos'][k - 1]
                if np.isfinite(vel) and abs(vel) > guard_v:
                    d.disable()
                    raise AbError('{:.0f} rpm passed the guard at {:.1f} s; drive disabled'
                                  .format(T.rpm(vel), sent[k]))
                if k >= 2 and np.isfinite(pos) and abs(sp[k - 2] - pos) > guard_err:
                    d.disable()
                    raise AbError('following error {:.0f} counts at {:.1f} s (limit {}); '
                                  'drive disabled'.format(sp[k - 2] - pos, sent[k], guard_err))
            if k - last_poll >= int(rate / 2):
                last_poll = k
                sw = d.sw()
                if sw & T.SW_FAULT:
                    raise AbError('drive faulted at {:.1f} s: {}'.format(
                        sent[k], T.cia402.diagnose(d.node)))
            wait = t0 + (k + 1) * period - d.now()
            if wait > 0:
                d.sleep(wait)
        meta['late_syncs'] = int(np.sum(np.diff(sent) > 1.5 * period))
    finally:
        try:
            _rpdo1(d, False)
        except Exception:       # noqa: BLE001
            pass
        d.stop()
        d.restore()
        try:
            d.unmap_pdos()
        finally:
            for name in names:
                try:
                    d.network.unsubscribe(PDO_MAP[name][2] + d.id)
                except Exception:       # noqa: BLE001
                    pass
    meta['bus_v_after'] = d.r(0x3000, 1, 'H')
    meta['temp_after'] = d.r(0x3000, 2, 'h')
    meta['tpdo_frames'] = dict(rec.count)
    meta['syncs'] = n
    emcy = [(t, c) for t, c, _, _ in d.emcy if c]
    meta['emcy'] = ['{:#06x}'.format(c) for _, c in emcy]
    data = {'sp': sp, 'sent_s': sent,
            'rep': np.concatenate([np.full(settle, -1), rep, np.full(settle, -1)])}
    data.update(rec.cols)
    return meta, data


def run(args, out=print):
    T = _drive_module()
    import canopen
    from p4core.can_backend import make_network
    from p4core.session import P4Network
    profiles = PROFILES if args.profile in (None, 'all') else \
        [p.strip() for p in args.profile.split(',') if p.strip()]
    bad = [p for p in profiles if p not in PROFILES]
    if bad:
        raise AbError('unknown profile(s): {} (have: {})'.format(', '.join(bad),
                                                                 ', '.join(PROFILES)))
    network = make_network(args.can, network_cls=P4Network)
    dirs = []
    try:
        node = network.add_node(args.id, canopen.ObjectDictionary())
        targs = T.parse_args(['--id', str(args.id)])
        d = T.Drive(network, node, targs, out=out)
        d.identify()
        network.subscribe(T.EMCY_BASE + d.id, d.on_emcy)
        out('node {}: firmware {} {}'.format(d.id, d.version, d.githash or ''))
        for profile in profiles:
            out('-- {}: {}'.format(profile, PROFILES[profile]['help']))
            d.emcy.clear()
            try:
                meta, data = run_profile(d, args.actuator, profile, args, out)
            except (T.TestFail, T.TestSkip, T.SdoAbort) as exc:
                raise AbError('{}: {}'.format(profile, exc)) from exc
            path = run_dir(args.runs, args.id, meta['fw'], profile)
            save_run(path, meta, data)
            met = metrics(analyse(data, meta))
            with open(os.path.join(path, 'metrics.json'), 'w') as f:
                json.dump(met, f, indent=2)
            out('   {}: pos err rms {:.3f} deg, peak {:.3f}{}; vel err rms {:.2f} deg/s; '
                'iq rms {:.0f} mA, peak {:.0f}; iq vs 0x6078 r {:+.3f}; TPDO {}'.format(
                    os.path.basename(path), met['pos_err_rms_deg'], met['pos_err_peak_deg'],
                    ' (lag {:.0f} ms, then rms {:.3f})'.format(
                        met['lag_ms'], met['pos_err_at_lag_rms_deg'])
                    if 'lag_ms' in met else '',
                    met['vel_err_rms_dps'], met['iq_rms_ma'], met['iq_peak_ma'],
                    met['iq_6078_corr'], meta['tpdo_frames']))
            # 4.3.5's 0x6078 follows the one-sample spikes of a step loosely (r ~0.3)
            if met['iq_6078_corr'] < 0 and 'lag_ms' in met:
                out('   WARNING: the alpha/beta -> iq reconstruction runs against 0x6078 '
                    '(r {:.3f}); check the calibration values in meta.json'.format(
                        met['iq_6078_corr']))
            if meta['emcy']:
                out('   EMCY during the run: ' + ', '.join(meta['emcy']))
            dirs.append(path)
            if args.plot:
                plot_runs([path], os.path.join(path, 'plot.png'))
    finally:
        network.disconnect()
    return dirs


# ---------------------------------------------------------------------------
# Plot and compare
# ---------------------------------------------------------------------------
def _label(meta):
    v = meta.get('version', '')
    return '{} n{}'.format(meta['fw'] if v == meta['fw'] else '{} ({})'.format(meta['fw'], v),
                           meta['node'])


def plot_runs(paths, png=None):
    """Id/Iq, velocity error and position error against time, one colour per
    run (A then B)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(5, 1, figsize=(12, 13), sharex=True)
    for i, path in enumerate(paths):
        meta, data = load_run(path)
        a = analyse(data, meta)
        c = 'C{}'.format(i)
        lab = _label(meta)
        t = a['t']
        ax[0].plot(t, a['ref_deg'] - a['sp_deg'][0], c, lw=1,
                   label=lab + ' set-point (delayed)')
        ax[1].plot(t, a['iq_ma'], c, lw=0.8, label=lab + ' iq')
        ax[2].plot(t, a['id_ma'], c, lw=0.8, label=lab + ' id')
        ax[3].plot(t, a['vel_err_dps'], c, lw=0.8, label=lab + ' vs d(0x6064)/dt')
        ax[3].plot(t, a['vel_err_606c_dps'], c, lw=0.5, ls=':', alpha=0.6,
                   label=lab + ' vs 0x606C')
        ax[4].plot(t, a['pos_err_deg'], c, lw=0.8, label=lab + ' set-point - 0x6064')
        if 'err60f4_deg' in a:
            ax[4].plot(t, a['err60f4_deg'], c, lw=0.6, ls='--', label=lab + ' 0x60F4')
    ax[0].set_ylabel('output deg\nfrom start')
    ax[1].set_ylabel('iq mA')
    ax[2].set_ylabel('id mA')
    ax[3].set_ylabel('vel err\noutput deg/s')
    ax[4].set_ylabel('pos err\noutput deg')
    ax[4].set_xlabel('s')
    for x in ax:
        x.grid(alpha=0.3)
        x.legend(fontsize=7, loc='upper right')
    meta0, _ = load_run(paths[0])
    labels = ' vs '.join(_label(load_run(p)[0]) for p in paths)
    fig.suptitle('{} {} ({}), CSP {:.0f} Hz'.format(meta0['actuator'], meta0['profile'],
                                                    labels, meta0['rate_hz']))
    fig.tight_layout()
    png = png or os.path.join(paths[-1], 'plot{}.png'.format('_ab' if len(paths) > 1 else ''))
    fig.savefig(png, dpi=110)
    plt.close(fig)
    return png


def compare(path_a, path_b, out=print):
    rows = []
    res = {}
    for key, path in (('A', path_a), ('B', path_b)):
        meta, data = load_run(path)
        res[key] = {'run': os.path.basename(path), 'fw': meta['fw'],
                    'version': meta.get('version'), 'profile': meta['profile'],
                    'metrics': metrics(analyse(data, meta))}
    if res['A']['profile'] != res['B']['profile']:
        out('WARNING: comparing {} with {}'.format(res['A']['profile'], res['B']['profile']))
    keys = [k for k in res['A']['metrics'] if k in res['B']['metrics']]
    out('{:<24} {:>14} {:>14}'.format('', _short(res['A']), _short(res['B'])))
    for k in keys:
        a, b = res['A']['metrics'][k], res['B']['metrics'][k]
        rows.append((k, a, b))
        out('{:<24} {:>14.4g} {:>14.4g}'.format(k, a, b))
    with open(os.path.join(path_b, 'compare.json'), 'w') as f:
        json.dump(res, f, indent=2)
    return res


def _short(r):
    return '{} {}'.format(r['fw'], r['version'])


# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split('\n', 1)[1])
    p.add_argument('--can', default=os.environ.get('P4_CAN', 'can0'),
                   help='CAN interface; on Windows the adapter index (default $P4_CAN or can0)')
    p.add_argument('--id', type=int, help='node ID (required for prepare, the stages and run)')
    p.add_argument('--runs', default=os.path.join(os.getcwd(), 'runs'),
                   help='where run directories go (default ./runs)')
    sub = p.add_subparsers(dest='cmd', metavar='COMMAND')
    sub.required = True

    def actuator(sp):
        sp.add_argument('--actuator', required=True, choices=sorted(ACTUATORS))

    sp = sub.add_parser('prepare', help='nv save, flash, wipe, load the CSV, calibrate, verify')
    sp.add_argument('--fw', required=True, choices=FIRMWARE)
    actuator(sp)
    sp.add_argument('--csv', help='the CSV (default: the actuator\'s newest in --csv-dir)')
    sp.add_argument('--csv-dir', default=os.path.join(HERE, 'csv'))
    sp.add_argument('--bin', help='the firmware image (default: the --fw one in --firmware-dir)')
    sp.add_argument('--firmware-dir', default=os.path.join(HERE, 'firmware'))
    sp.add_argument('--no-flash', action='store_true', help='the node already runs --fw')

    sp = sub.add_parser('flash', help='program a firmware image')
    sp.add_argument('file')
    sp = sub.add_parser('wipe', help='erase NV, keeping identity, node ID and CAN settings')
    sp.add_argument('--backup', required=True, help='CSV (5.x) or NV JSON (4.3.5) of what was there')
    sp = sub.add_parser('load', help='write a CSV, save and reboot')
    sp.add_argument('csv')
    sp = sub.add_parser('calibrate', help='bias, gain, encoder zero + winding order (default: all)')
    sp.add_argument('steps', nargs='*', default=['ab'],
                    help='offsets gainfactor encoder-offset ezero, or ab for all four')

    sp = sub.add_parser('run', help='the CSP trajectories, captured')
    actuator(sp)
    sp.add_argument('--profile', default='all',
                    help='NAME[,NAME...] or all: ' + ', '.join(PROFILES))
    sp.add_argument('--reps', type=int, default=REPS, help='repetitions (default 2)')
    sp.add_argument('--settle', type=float, default=1.0,
                    help='seconds held before and after the trajectory (default 1)')
    sp.add_argument('--max-error-revs', type=float, default=4.0,
                    help='disable the drive past this following error, motor revs (default 4)')
    sp.add_argument('--no-plot', dest='plot', action='store_false')

    sp = sub.add_parser('plot', help='plot one run, or two overlaid')
    sp.add_argument('rundirs', nargs='+')
    sp.add_argument('-o', '--output', help='PNG (default: in the last run directory)')
    sp = sub.add_parser('compare', help='metrics of two runs side by side')
    sp.add_argument('run_a')
    sp.add_argument('run_b')
    sp = sub.add_parser('profiles', help='list the profiles')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    needs_id = args.cmd in ('prepare', 'flash', 'wipe', 'load', 'calibrate', 'run')
    if needs_id and args.id is None:
        raise SystemExit('--id is required for ' + args.cmd)
    try:
        if args.cmd == 'prepare':
            prepare(args)
        elif args.cmd in ('flash', 'wipe', 'load', 'calibrate'):
            st = Stages(args.can, args.id)
            if args.cmd == 'flash':
                st.flash(args.file)
            elif args.cmd == 'wipe':
                st.wipe(args.backup)
            elif args.cmd == 'load':
                st.load(args.csv)
            else:
                st.calibrate(args.steps)
        elif args.cmd == 'run':
            run(args)
        elif args.cmd == 'plot':
            print(plot_runs(args.rundirs, args.output))
        elif args.cmd == 'compare':
            compare(args.run_a, args.run_b)
        elif args.cmd == 'profiles':
            for name, p in PROFILES.items():
                print('{:<8} {}'.format(name, p['help']))
    except AbError as exc:
        print('ERROR: {}'.format(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
