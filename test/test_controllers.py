"""The controllers over fakes: the headless calibration sequence and
system-config (the GUI's flash/config children are p4core.worker's)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'p4core', 'tests'))

from p4core import config_csv, ops                            # noqa: E402
from p4core.reporter import RecordingReporter                  # noqa: E402

from puckutility import paths                                  # noqa: E402
from puckutility.controllers import calibrate, system_config  # noqa: E402


class Libs:
    """Patches every library call the controller makes and records them."""

    def __init__(self, case, results=None):
        self.calls = []
        self.results = results or {}
        targets = {
            'clear_slope': (calibrate.cal, 'clear_slope'),
            'clear_offset': (calibrate.drivecal, 'clear_offset_reg'),
            'test_encoder': (calibrate.enctest, 'test_encoder'),
            'bias': (calibrate.cal, 'calibrate_bias'),
            'gain': (calibrate.cal, 'calibrate_gain'),
            'slope': (calibrate.slope, 'calibrate_current_slope'),
            'deenergise': (calibrate.cal, 'deenergise'),
            'enczero': (calibrate.cal, 'calibrate_ezero'),
            'fold': (calibrate.slope, 'fold_baseline_offset'),
            'settling': (calibrate.settling, 'calibrate_settling'),
            'fault': (calibrate.drivecal, 'fault_message'),
        }
        for name, (module, attr) in targets.items():
            p = mock.patch.object(module, attr, side_effect=self._fake(name))
            p.start()
            case.addCleanup(p.stop)

    def _fake(self, name):
        def call(*args, **kw):
            self.calls.append((name, kw))
            result = self.results.get(name)
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, BaseException):
                raise result
            if name == 'fault':
                return 'fault: {}'.format(args[1])
            if name == 'slope' and result is None:
                return dict(stored=True, skipped=None, a0=1.0, b0=2.0)
            if name == 'settling' and result is None:
                return dict(slope_cleared=False)
            return result
        return call

    def names(self):
        return [n for n, _kw in self.calls]


class TestCalibrate(unittest.TestCase):

    def setUp(self):
        self.node = mock.MagicMock()
        self.node.id = 5

    def test_the_full_sequence(self):
        libs = Libs(self)
        self.assertTrue(calibrate.calibrate_all(self.node))
        self.assertEqual(libs.names(), ['clear_slope', 'clear_offset',
                                        'test_encoder', 'bias', 'gain',
                                        'slope', 'enczero', 'fold'])
        kw = dict(libs.calls)
        self.assertEqual(kw['bias'], calibrate.cal.BIAS_MENU)
        self.assertEqual(kw['gain'], calibrate.cal.GAIN_MENU)
        self.assertEqual(kw['slope'], dict(force_sdo=False, quick=False))
        self.assertEqual(kw['enczero'], dict(steps=16))

    def test_quick_passes_quick(self):
        libs = Libs(self)
        self.assertTrue(calibrate.calibrate_all(self.node, quick=True))
        kw = dict(libs.calls)
        self.assertEqual(kw['bias'], calibrate.cal.BIAS_MENU_QUICK)
        self.assertTrue(kw['slope']['quick'])
        self.assertEqual(kw['enczero'],
                         dict(steps=calibrate.cal.EZERO_QUICK_STEPS))

    def test_a_declined_step_stops_the_sequence(self):
        libs = Libs(self, {'gain': calibrate.cal.CalibrationDeclined('bounds')})
        rep = RecordingReporter()
        self.assertFalse(calibrate.calibrate_all(self.node, reporter=rep))
        self.assertEqual(libs.names()[-1], 'gain')
        self.assertTrue(any('current gain' in w for w in rep.texts('warn')))

    def test_a_slope_failure_is_retried_then_skipped(self):
        libs = Libs(self, {'slope': [RuntimeError('SYNC'), RuntimeError('SDO')]})
        rep = RecordingReporter()
        self.assertTrue(calibrate.calibrate_all(self.node, reporter=rep))
        slopes = [kw for n, kw in libs.calls if n == 'slope']
        self.assertEqual([kw['force_sdo'] for kw in slopes], [False, True])
        self.assertNotIn('fold', libs.names())

    def test_a_fault_is_reported_and_fails(self):
        libs = Libs(self, {'bias': OSError('SDO timeout')})
        rep = RecordingReporter()
        self.assertFalse(calibrate.calibrate_all(self.node, reporter=rep))
        self.assertEqual(libs.names()[-1], 'fault')
        self.assertIn('fault: SDO timeout', rep.texts('warn'))

    def test_enczero_failure_asks(self):
        Libs(self, {'enczero': calibrate.cal.CalibrationError('stalled')})
        self.assertFalse(calibrate.calibrate_all(
            self.node, reporter=RecordingReporter(answers=[False])))

    def test_single_steps(self):
        libs = Libs(self)
        for step, lib in (('encoder', 'test_encoder'), ('bias', 'bias'),
                          ('gain', 'gain'), ('enczero', 'enczero'),
                          ('slope', 'slope'), ('settling', 'settling')):
            self.assertTrue(calibrate.run_step(step, self.node))
            self.assertEqual(libs.names()[-1], lib)
        libs.results['slope'] = dict(skipped='firmware', stored=False)
        self.assertFalse(calibrate.run_step('slope', self.node))
        with self.assertRaises(calibrate.CalibrationFailed):
            calibrate.run_step('bogus', self.node)

    def test_no_wx(self):
        """The controller and the libraries it calls import without wx."""
        import subprocess
        code = ('import sys; sys.modules["wx"] = None; '
                'import puckutility.controllers.calibrate')
        r = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class FakeSession:
    port = 'fake0'

    def __init__(self, found, versions):
        self.found = found
        self.versions = versions

    def scan(self):
        return self.found

    def read_version(self, node_id):
        return self.versions[node_id]


class TestSystemConfig(unittest.TestCase):

    INI = """
[Shoulder]
ID = 1
CSV = shoulder.csv
fw_version = 4.4.0
fw = P4-4.4.0.bin

[Elbow]
ID = 2
CSV = /abs/elbow.csv

[Wrist]
ID = 3
CSV = wrist.csv
"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ini = os.path.join(tmp.name, 's.ini')
        with open(self.ini, 'w') as f:
            f.write(self.INI)

    def test_read_ini_resolves_bare_names(self):
        pucks = system_config.read_ini(self.ini)
        self.assertEqual([p['node_id'] for p in pucks], [1, 2, 3])
        self.assertEqual(pucks[0]['csv'],
                         os.path.join(paths.CONFIG_DIR, 'shoulder.csv'))
        self.assertEqual(pucks[0]['fw'],
                         os.path.join(paths.FIRMWARE_DIR, 'P4-4.4.0.bin'))
        self.assertEqual(pucks[1]['csv'], '/abs/elbow.csv')
        self.assertIsNone(pucks[1]['fw'])

    def test_apply(self):
        session = FakeSession([1, 2], {1: '4.3.0', 2: '4.4.0'})
        ok = config_csv.ApplyResult()
        calibrated = []
        rep = RecordingReporter(answers=[True])
        with mock.patch.object(ops, 'flash') as flash, \
                mock.patch.object(ops, 'load_config', return_value=ok) as load:
            outcome = system_config.apply(session, self.ini, rep,
                                          calibrate=calibrated.append)
        self.assertEqual(outcome, {1: 'configured', 2: 'configured',
                                   3: 'missing'})
        flash.assert_called_once()
        self.assertEqual(flash.call_args.args[1], 1)
        self.assertEqual([c.args[1] for c in load.call_args_list], [1, 2])
        self.assertEqual(calibrated, [1, 2])

    def test_a_failed_flash_skips_that_config_and_calibration_is_opt_in(self):
        session = FakeSession([1, 2], {1: '4.3.0'})
        calibrated = []
        with mock.patch.object(ops, 'flash',
                               side_effect=ops.OperationError('CRC')), \
                mock.patch.object(ops, 'load_config',
                                  return_value=config_csv.ApplyResult()) as load:
            outcome = system_config.apply(session, self.ini,
                                          RecordingReporter(),
                                          calibrate=calibrated.append)
        self.assertEqual(outcome[1], 'flash failed')
        self.assertEqual([c.args[1] for c in load.call_args_list], [2])
        self.assertEqual(calibrated, [])          # not confirmed: default no

    def test_a_bad_ini(self):
        with open(self.ini, 'w') as f:
            f.write('[X]\nCSV = a.csv\n')
        with self.assertRaises(system_config.SystemConfigError):
            system_config.read_ini(self.ini)


if __name__ == '__main__':
    unittest.main()
