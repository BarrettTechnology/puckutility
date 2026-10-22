"""The controllers over fakes: the headless actuator calibration and
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


class FakePlanSession:
    """Stands in for a p4sysid steps.Session under plans.run/save: records
    the keys each run gets, passes them unless told to fail one."""

    def __init__(self, fail=()):
        self.runs = []
        self.results = {}
        self.unsaved = False
        self.fail = set(fail)

    def run(self, keys, on_update=None):
        self.runs.append(list(keys))
        ok = True
        for k in keys:
            self.results[k] = {'status': 'running', 'detail': ''}
            on_update(k)
            if not ok:
                status = 'skipped'
            elif k in self.fail:
                status, ok = 'fail', False
            else:
                status = 'pass'
                self.unsaved = k != 'save'
            self.results[k] = {'status': status, 'detail': k + ' detail'}
            on_update(k)
        return ok


class TestCalibrate(unittest.TestCase):

    def setUp(self):
        self.node = mock.MagicMock()
        self.node.id = 5

    def make(self, fail=()):
        self.session = FakePlanSession(fail)
        p = mock.patch.object(calibrate.plans, 'make_session',
                              return_value=self.session)
        self.make_session = p.start()
        self.addCleanup(p.stop)

    def test_the_steps_are_the_actuator_plan_numbered(self):
        self.assertEqual(list(calibrate.STEPS), [
            'encoder', 'bias', 'gain', 'slope', 'fold', 'winding', 'runout',
            'cogging', 'velocity_refine', 'position_refine'])
        self.assertEqual(set(calibrate.STEPS), set(calibrate.plans.ACTUATOR))
        self.assertEqual(calibrate.STEPS['bias'], 'Current-sense bias')

    def test_the_whole_calibration_in_ram(self):
        self.make()
        rep = RecordingReporter()
        reboot = mock.Mock()
        ok, session = calibrate.calibrate_actuator(self.node, rep,
                                                   reboot=reboot)
        self.assertTrue(ok)
        self.assertIs(session, self.session)
        self.assertEqual(self.session.runs,
                         [list(calibrate.plans.run_order(
                             calibrate.plans.ACTUATOR))])
        self.assertIs(self.make_session.call_args.args[0], self.node)
        self.assertIs(self.make_session.call_args.kwargs['reboot'], reboot)
        self.assertTrue(session.unsaved)          # not saved: not asked
        self.assertIn('Encoder runout table', calibrate.summary_text(session))

    def test_save_only_when_asked_and_passed(self):
        self.make()
        ok, session = calibrate.calibrate_actuator(self.node, save=True)
        self.assertTrue(ok)
        self.assertEqual(self.session.runs[-1], ['save'])
        self.assertFalse(session.unsaved)
        self.make(fail=('winding',))
        ok, session = calibrate.calibrate_actuator(self.node, save=True)
        self.assertFalse(ok)
        self.assertEqual(len(self.session.runs), 1)   # no Save.All
        self.assertEqual(session.results['runout']['status'], 'skipped')

    def test_named_steps_run_in_order_in_a_given_session(self):
        self.make()
        session = FakePlanSession()
        ok, out = calibrate.calibrate_actuator(
            self.node, steps=['fold', 'ezero', 'slope'], session=session)
        self.assertTrue(ok)
        self.assertIs(out, session)
        self.assertEqual(session.runs, [['slope', 'fold', 'winding']])
        self.make_session.assert_not_called()

    def test_batch_and_unknown_steps_are_refused(self):
        for name in ('settling', 'latency', 'pwm', 'quick', 'save', 'bogus'):
            with self.assertRaises(calibrate.CalibrationFailed):
                calibrate.step_keys([name])
        self.assertEqual(calibrate.step_keys(['encoder-test', 'enczero']),
                         ['encoder', 'winding'])

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
