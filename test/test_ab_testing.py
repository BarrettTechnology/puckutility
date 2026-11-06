"""abtest/ab_testing.py: the trajectories stay inside the actuator limits,
alpha/beta -> dq inverts the firmware's conversion, the calibration gate,
finding the CSV, the p4core stages, and a run against a fake puck on both
firmware lines (PDOs mapped and put back, data saved, plotted, compared)."""

import math
import os
import struct
import sys
import tempfile
import types
import unittest

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'sandbox'))
sys.path.insert(0, os.path.dirname(__file__))

from abtest import ab_testing as A                           # noqa: E402
import test_testp4 as F                                      # noqa: E402
import testp4 as T                                           # noqa: E402

CAL = {'bias_a': 2000.0, 'bias_b': 2010.0, 'gf_a': 4096, 'gf_b': 4195,
       'gain': 20000, 'shunt': 5}


def raw_from_dq(i_d, i_q, theta, cal):
    """The ADC counts the firmware would read for (id, iq) at *theta*."""
    th = np.asarray(theta, float) * math.pi / 32768
    ia = i_d * np.cos(th) - i_q * np.sin(th)
    ib = i_d * np.sin(th) + i_q * np.cos(th)
    k = A.ma_per_count(cal['gain'], cal['shunt'])
    return (cal['bias_a'] - ia / (cal['gf_a'] / 4096.0 * k),
            cal['bias_b'] - ib / (cal['gf_b'] / 4096.0 * k))


class TestTrajectories(unittest.TestCase):
    def test_every_profile_stays_inside_the_limits_and_comes_back(self):
        for act in A.ACTUATORS:
            vmax, amax = A.output_limits(act)
            k = A.cts_per_deg(act)
            step_max = 0.5 * A.ACTUATORS[act]['vmax'] / A.RATE_HZ
            for name, p in A.PROFILES.items():
                x, rep = A.trajectory(name, act)
                self.assertEqual(len(x), len(rep))
                self.assertEqual(x[0], 0.0, (act, name))
                self.assertAlmostEqual(x[-1], 0.0, places=6, msg=(act, name))
                self.assertLessEqual(np.abs(np.diff(np.round(x * k))).max(), step_max,
                                     (act, name))
                if p['kind'] in ('chirp', 'ramp'):
                    v = np.diff(x) * A.RATE_HZ
                    self.assertLessEqual(np.abs(v).max(), min(vmax, 91.0), (act, name))
                    self.assertLessEqual(np.abs(np.diff(v)).max() * A.RATE_HZ,
                                         1.01 * amax, (act, name))

    def test_both_directions(self):
        for name in ('chirp', 'square', 'ramp'):
            x, _ = A.trajectory(name, 'BO4523')
            self.assertLess(x.min(), -0.4 if name != 'ramp' else 1, name)
            self.assertGreater(x.max(), 0.4, name)
        v = np.diff(A.trajectory('ramp', 'BO3518')[0])
        self.assertLess(v.min(), 0)
        self.assertGreater(v.max(), 0)

    def test_the_same_motion_on_both_firmwares(self):
        a, _ = A.trajectory('chirp', 'BO4523', reps=1)
        b, _ = A.trajectory('chirp', 'BO4523', reps=1)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(len(A.trajectory('square', 'BO4523', reps=2)[0]),
                         2 * len(A.trajectory('square', 'BO4523', reps=1)[0]))


class TestCurrents(unittest.TestCase):
    def test_park_inverts_the_firmware_conversion(self):
        theta = np.linspace(-32768, 32767, 400)
        i_d = 100 + 50 * np.sin(theta / 5000.0)
        i_q = 1500 * np.cos(theta / 7000.0)
        raw_a, raw_b = raw_from_dq(i_d, i_q, theta, CAL)
        ia, ib = A.alpha_beta_ma(raw_a, raw_b, CAL)
        d, q = A.park(ia, ib, theta)
        np.testing.assert_allclose(d, i_d, atol=1e-6)
        np.testing.assert_allclose(q, i_q, atol=1e-6)

    def test_bias_reads_as_zero_current(self):
        ia, ib = A.alpha_beta_ma([2000], [2010], CAL)
        self.assertEqual((ia[0], ib[0]), (0.0, 0.0))
        ia, _ = A.alpha_beta_ma([1999], [2010], CAL)
        self.assertGreater(ia[0], 0)            # below the bias = current into the phase


class TestAnalysis(unittest.TestCase):
    def run_data(self, n=300, flip=False):
        kdeg = A.cts_per_deg('BO4523')
        x, _ = A.trajectory('chirp', 'BO4523', reps=1)
        sp = np.round(x[:n] * kdeg)
        pos = np.full(n, np.nan)
        pos[1:] = sp[:-1]                       # reaches set-point k at SYNC k+1
        vel = np.full(n, np.nan)
        vel[2:] = (sp[2:] - sp[:-2]) * 25.0
        theta = (np.nan_to_num(pos) * 7 * 16) % 65536 - 32768
        iq = np.nan_to_num(vel) / 50.0
        raw_a, raw_b = raw_from_dq(np.zeros(n), iq, theta, CAL)
        i6078 = (-iq if flip else iq) / 10
        data = {'sp': sp, 'pos': pos, 'vel': vel, 'raw_a': raw_a, 'raw_b': raw_b,
                'theta': theta, 'i6078': i6078}
        meta = {'rate_hz': 50.0, 'cts_per_deg': kdeg, 'cal': CAL}
        return data, meta, iq

    def test_perfect_tracking_has_no_error(self):
        data, meta, iq = self.run_data()
        a = A.analyse(data, meta)
        self.assertLess(np.nanmax(np.abs(a['pos_err_deg'])), 1e-9)
        self.assertLess(np.nanmax(np.abs(a['vel_err_606c_dps'])), 1e-9)
        self.assertLess(np.nanmax(np.abs(a['vel_err_dps'][2:])), 1e-9)
        np.testing.assert_allclose(a['iq_ma'], iq, atol=1e-6)
        self.assertEqual(a['iq_sign'], 1.0)
        m = A.metrics(a)
        self.assertLess(m['pos_err_rms_deg'], 1e-9)
        self.assertGreater(m['iq_6078_corr'], 0.99)

    def test_iq_takes_the_sign_that_does_work_not_that_of_0x6078(self):
        data, meta, iq = self.run_data(flip=True)
        a = A.analyse(data, meta)
        self.assertEqual(a['iq_sign'], 1.0)
        np.testing.assert_allclose(a['iq_ma'], iq, atol=1e-6)
        self.assertLess(a['iq_6078_corr'], -0.99)          # the check says so
        data['raw_a'], data['raw_b'] = raw_from_dq(np.zeros(len(iq)), -iq, data['theta'], CAL)
        a = A.analyse(data, meta)
        self.assertEqual(a['iq_sign'], -1.0)
        np.testing.assert_allclose(a['iq_ma'], iq, atol=1e-6)

    def test_iq_takes_the_sign_of_the_drive_iq_on_5x(self):
        # 5.x square steps: the sampled work comes out negative, the drive's
        # iq (0x3010,9) still gives the sign
        data, meta, iq = self.run_data()
        data['raw_a'], data['raw_b'] = raw_from_dq(np.zeros(len(iq)), -iq, data['theta'], CAL)
        data['iq_fw'] = -iq
        a = A.analyse(data, meta)
        self.assertEqual(a['iq_sign'], 1.0)
        self.assertGreater(a['iq_fw_corr'], 0.99)

    def test_a_lag_shows_as_position_error_and_is_fitted(self):
        data, meta, _ = self.run_data()
        m = A.metrics(A.analyse(data, meta))
        self.assertAlmostEqual(m['lag_ms'], 20.0)           # sp[k-1]: one SYNC
        data['pos'] = np.concatenate([[np.nan, np.nan], data['sp'][:-2]])
        m = A.metrics(A.analyse(data, meta))
        self.assertGreater(m['pos_err_rms_deg'], 0.01)
        self.assertAlmostEqual(m['lag_ms'], 40.0)
        self.assertLess(m['pos_err_at_lag_rms_deg'], 1e-9)


class TestCalibrationGate(unittest.TestCase):
    def test_calibrated_passes(self):
        A.check_calibrated(CAL, legacy=True)

    def test_a_5x_bias_on_435_is_refused(self):
        cal = dict(CAL, bias_a=32000.0)
        with self.assertRaisesRegex(A.AbError, 'bias a'):
            A.check_calibrated(cal, legacy=True)

    def test_a_zero_gain_factor_is_refused(self):
        with self.assertRaisesRegex(A.AbError, 'gain factor b = 0'):
            A.check_calibrated(dict(CAL, gf_b=0), legacy=False)


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def touch(self, name):
        path = os.path.join(self.tmp.name, name)
        open(path, 'w').close()
        return path

    def test_find_csv_takes_the_newest_version_of_the_right_one(self):
        self.touch('P4-42-42V-15A-BO4523-HFM-fw5.0.5-v1.0.0-2026-11-05.csv')
        new = self.touch('P4-42-42V-15A-BO4523-HFM-fw5.0.5-v1.10.0-2026-11-06.csv')
        self.touch('P4-42-42V-15A-BO4523-HFM-fw4.3.5-v1.20.0-2026-11-06.csv')
        self.touch('P4-32-42V-7A-BO3518-fw5.0.5-v9.0.0-2026-11-06.csv')
        self.assertEqual(A.find_csv('BO4523', '5.0.5', self.tmp.name), new)
        with self.assertRaises(A.AbError):
            A.find_csv('BO3518', '4.3.5', self.tmp.name)

    def test_find_firmware_takes_the_bin_before_the_ebin(self):
        self.touch('puck4.app.stm32.release.v5.0.5-0-gc38572a.ebin')
        self.touch('puck4.app.stm32.release.v4.3.5-0-g6605fba.bin')
        self.assertTrue(A.find_firmware('5.0.5', self.tmp.name).endswith('.ebin'))
        b = self.touch('puck4.app.stm32.release.v5.0.5-0-gc38572a.bin')
        self.assertEqual(A.find_firmware('5.0.5', self.tmp.name), b)
        with self.assertRaises(A.AbError):
            A.find_firmware('4.3.3', self.tmp.name)

    def test_the_package_csvs_are_found(self):
        # the bench drafts, when this checkout sits in p4gui/worktrees/
        csv_dir = os.path.join(ROOT, '..', '..', '..', 'notes', 'bench', 'abtest', 'csv')
        if not os.path.isdir(csv_dir):
            self.skipTest('no notes/bench/abtest/csv')
        for act in A.ACTUATORS:
            for fw in A.FIRMWARE:
                self.assertIn(fw, os.path.basename(A.find_csv(act, fw, csv_dir)))

    def test_fw_line(self):
        self.assertEqual(A.fw_line('4.3.5'), '4.3.5')
        self.assertEqual(A.fw_line('5.0.4'), '5.0.5')

    def test_save_and_load_a_run(self):
        path = A.run_dir(self.tmp.name, 4, '4.3.5', 'hold')
        self.assertIn('-n4-4.3.5-hold', path)
        A.save_run(path, {'x': np.float64(1.5), 'n': np.int64(3)}, {'sp': [1, 2]})
        meta, data = A.load_run(path)
        self.assertEqual(meta, {'x': 1.5, 'n': 3})
        np.testing.assert_array_equal(data['sp'], [1, 2])


class TestStages(unittest.TestCase):
    def test_commands_and_log(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='10%\n4.3.5 n/a\n', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'p.log')
            st = A.Stages('can3', 13, log, out=lambda s: None, runner=runner)
            self.assertEqual(st.version(), ('4.3.5', 'n/a'))
            st.calibrate()
            with open(log) as f:
                text = f.read()
        self.assertEqual(calls[0][1:], ['-m', 'p4core', '--can', 'can3', '--id', '13', '-y',
                                        'version'])
        self.assertEqual(calls[1][-2:], ['calibrate', 'ab'])
        self.assertNotIn('10%', text)
        self.assertIn('exit 0', text)

    def test_a_failing_stage_stops(self):
        def runner(cmd, **kw):
            return types.SimpleNamespace(returncode=2, stdout='', stderr='boom')
        st = A.Stages('can3', 13, out=lambda s: None, runner=runner)
        with self.assertRaisesRegex(A.AbError, 'wipe-nvm failed'):
            st.wipe('x.csv')


# ---------------------------------------------------------------------------
# A run against the fake puck: CSP that reaches set-point k at SYNC k+1
# ---------------------------------------------------------------------------
class CspNetwork(F.FakeNetwork):
    """RPDO1 = (controlword, target); at each SYNC the position becomes the
    previous SYNC's target and the TPDOs of the A/B layout go out."""

    def __init__(self, sdo, node_id=4, legacy=False):
        super().__init__(sdo, node_id)
        self.legacy = legacy
        self.target = None
        self.latched = None
        self.unsubscribed = []

    def unsubscribe(self, cob, cb=None):
        self.unsubscribed.append(cob)
        self.subs.pop(cob, None)

    def send_message(self, cob, data):
        self.sent.append((cob, bytes(data)))
        s = self.sdo
        if cob == 0x200 + self.id:
            self.target = struct.unpack('<Hi', data)[1]
            return
        if cob != 0x80:
            return
        old = s.get(0x6064, 0, 'i')
        if self.latched is not None:
            s.put(0x6064, 0, 'i', self.latched)
        self.latched = self.target
        pos = s.get(0x6064, 0, 'i')
        vel = (pos - old) * 50
        theta = (pos * 7 * 16) % 65536 - 32768
        iq = max(-30000.0, min(30000.0, vel / 100.0))
        ra, rb = raw_from_dq(0.0, iq, theta, CAL_FW)
        frames = {0x180: struct.pack('<HHhh', int(round(ra)), int(round(rb)), theta,
                                     int(-iq / 10)),
                  0x280: struct.pack('<ii', pos, vel)}
        if not self.legacy:
            frames[0x380] = struct.pack('<ihh', 0, int(iq), 0)
        for base, payload in frames.items():
            for cb in self.subs.get(base + self.id, []):
                cb(base + self.id, payload, 0)


CAL_FW = dict(CAL)


def make_fake(legacy):
    sdo = F.FakeSdo(legacy)
    v = lambda fmt, x: struct.pack('<' + fmt, x)       # noqa: E731
    q = 1 if legacy else 16
    sdo.store.update({
        (0x3008, 3): v('H', int(CAL['bias_a'] * q)), (0x3009, 3): v('H', int(CAL['bias_b'] * q)),
        (0x3008, 4): v('H', CAL['gain']), (0x3008, 5): v('H', CAL['shunt']),
        (0x3008, 6): v('H', CAL['gf_a']), (0x3009, 6): v('H', CAL['gf_b']),
        (0x6091, 1): v('I', 264), (0x6091, 2): v('I', 10),
        (0x3000, 1): v('H', 42000), (0x3000, 2): v('h', 31),
    })
    for comm, mapping, cob in ((0x1802, 0x1A02, 0x380),):
        sdo.store[(comm, 1)] = v('I', cob + 4)
        sdo.store[(comm, 2)] = v('B', 1)
        sdo.store[(mapping, 0)] = v('B', 0)
    clock = F.Clock()
    clock.plant = sdo
    node = F.FakeNode(sdo)
    net = CspNetwork(sdo, legacy=legacy)
    lines = []
    d = T.Drive(net, node, T.parse_args(['--id', '4']), clock=clock.now,
                sleep=clock.sleep, out=lines.append)
    d.identify()
    return d, sdo, net


class TestRun(unittest.TestCase):
    ARGS = types.SimpleNamespace(reps=1, settle=0.2, max_error_revs=4.0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_one(self, legacy, profile='square'):
        d, sdo, net = make_fake(legacy)
        before = dict(sdo.store)
        meta, data = A.run_profile(d, 'BO4523', profile, self.ARGS, out=lambda s: None)
        return d, sdo, net, before, meta, data

    def test_both_lines(self):
        paths = []
        for legacy in (True, False):
            d, sdo, net, before, meta, data = self.run_one(legacy)
            n = meta['syncs']
            syncs = [c for c, _ in net.sent if c == 0x80]
            self.assertEqual(len(syncs), n)
            self.assertEqual(meta['fw'], '4.3.5' if legacy else '5.0.5')
            self.assertEqual(meta['tpdo_frames']['tpdo1'], n)
            self.assertEqual('tpdo3' in meta['tpdo_frames'], not legacy)
            self.assertEqual(meta['cal']['bias_a'], CAL['bias_a'])
            # the mapping and COB-IDs went back, RPDO1 invalid again
            for key in ((0x1A00, 1), (0x1A01, 1), (0x1A00, 0), (0x1800, 1), (0x1400, 1)):
                self.assertEqual(sdo.store[key], before[key], hex(key[0]))
            self.assertEqual(sdo.store[(0x60C2, 1)], before[(0x60C2, 1)])
            self.assertEqual(sdo.sw() & 0x6F, 0x21)         # Ready to Switch On
            a = A.analyse(data, meta)
            self.assertLess(np.nanmax(np.abs(a['pos_err_deg'])), 1e-9)
            self.assertLess(a['iq_6078_corr'], -0.99)     # the fake's 0x6078 is -iq
            self.assertEqual(a['iq_sign'], 1.0)
            path = A.run_dir(self.tmp.name, 4, meta['fw'], 'square')
            A.save_run(path, meta, data)
            paths.append(path)
        png = A.plot_runs(paths, os.path.join(self.tmp.name, 'ab.png'))
        self.assertGreater(os.path.getsize(png), 10000)
        res = A.compare(paths[0], paths[1], out=lambda s: None)
        self.assertEqual((res['A']['fw'], res['B']['fw']), ('4.3.5', '5.0.5'))
        self.assertTrue(os.path.exists(os.path.join(paths[1], 'compare.json')))

    def test_the_wrong_gear_ratio_is_refused(self):
        d, sdo, net = make_fake(False)
        sdo.put(0x6091, 1, 'I', 17830)
        sdo.put(0x6091, 2, 'I', 1000)
        with self.assertRaisesRegex(A.AbError, '0x6091'):
            A.run_profile(d, 'BO4523', 'hold', self.ARGS, out=lambda s: None)
        self.assertNotIn(0x80, [c for c, _ in net.sent])

    def test_an_uncalibrated_drive_is_not_enabled(self):
        d, sdo, net = make_fake(True)
        sdo.put(0x3008, 3, 'H', 32000)          # 5.x's Q12.4 bias read by 4.3.5
        with self.assertRaises(A.AbError):
            A.run_profile(d, 'BO4523', 'hold', self.ARGS, out=lambda s: None)
        self.assertNotIn((0x6040, 0, struct.pack('<H', T.CW_ENABLE)),
                         sdo.writes)

    def test_a_runaway_is_stopped(self):
        d, sdo, net = make_fake(False)
        orig = net.send_message

        def runaway(cob, data):
            if cob == 0x200 + net.id:
                tgt = struct.unpack('<Hi', data)[1]
                data = struct.pack('<Hi', T.CW_ENABLE, tgt + 100000)
            orig(cob, data)
        net.send_message = runaway
        with self.assertRaisesRegex(A.AbError, 'guard|following error'):
            A.run_profile(d, 'BO4523', 'hold', self.ARGS, out=lambda s: None)
        self.assertEqual(sdo.sw() & 0x6F, 0x21)


if __name__ == '__main__':
    unittest.main()
