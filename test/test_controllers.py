"""The controllers over fakes: the GUI's flash/config children, the headless
calibration sequence, and system-config."""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'p4core', 'tests'))

from p4core import config_csv, ops                            # noqa: E402
from p4core import flash as _flash                             # noqa: E402
from p4core.reporter import RecordingReporter                  # noqa: E402

from puckutility import paths                                  # noqa: E402
from puckutility.controllers import calibrate, device, system_config  # noqa: E402


class Queue(list):
    put = list.append


class TestDeviceChildren(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch('builtins.print')      # the children log to stdout
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flash_refuses_golden_and_says_fail(self):
        q = Queue()
        with mock.patch.object(_flash, 'flash_port') as flash_port:
            device.flash_child('can0', 5, '/x/P4-GOLDEN-4.4.0.bin', q)
        flash_port.assert_not_called()
        self.assertEqual(q, ['Fail'])

    def test_flash_pass(self):
        q = Queue()

        def fake(port, node_id, path, reporter):
            reporter.progress(0)
            reporter.progress(50)
            reporter.progress(50)          # repeats are not queued
            reporter.progress(100)
            return _flash.FlashResult.SUCCESS

        with mock.patch.object(_flash, 'flash_port', side_effect=fake):
            device.flash_child('can0', 5, 'app.bin', q)
        self.assertEqual(q, [0, 50, 100, 'Pass'])

    def test_flash_failure_and_exception_say_fail(self):
        for effect in (_flash.FlashResult.CRC_MISMATCH, OSError('bus')):
            q = Queue()
            with mock.patch.object(_flash, 'flash_port', side_effect=[effect]
                                   if isinstance(effect, Exception) else None,
                                   return_value=effect), \
                    mock.patch('traceback.print_exc'):
                device.flash_child('can0', 5, 'app.bin', q)
            self.assertEqual(q[-1], 'Fail')

    def _config(self, result=None, error=None):
        q = Queue()
        network = mock.Mock()
        load = mock.Mock(return_value=result, side_effect=error)
        with mock.patch('p4core.can_backend.make_network',
                        return_value=network), \
                mock.patch.object(config_csv, 'load', load):
            device.config_child('can0', 7, 'm.csv', q)
        network.disconnect.assert_called_once()
        return q, load

    def test_config_writes_only(self):
        result = config_csv.ApplyResult()
        result.written = 3
        q, load = self._config(result)
        self.assertEqual(q, ['Pass'])
        self.assertEqual(load.call_args.args[1:3], ('m.csv', 7))

    def test_config_errors_or_refusal_say_fail(self):
        result = config_csv.ApplyResult()
        result.errors = ['line 3: bad']
        self.assertEqual(self._config(result)[0], ['Fail'])
        self.assertEqual(self._config(error=config_csv.ConfigError(
            []))[0], ['Fail'])


class FakeCalibrate:
    """Stands in for the calibrate_menu mixin; records what ran."""

    results = {}

    created = []

    def _run(self, name, kwargs):
        # HeadlessCalibrator.__init__ replaces ours: start the record here.
        if 'calls' not in self.__dict__:
            self.calls = []
            self.created.append(self)
        self.calls.append((name, kwargs))
        return self.results.get(name, True)

    def test_encoder(self, event, **kw):
        return self._run('test_encoder', kw)

    def calibrate_ibias(self, event, **kw):
        return self._run('ibias', kw)

    def calibrate_igainfactor(self, event, **kw):
        return self._run('gain', kw)

    def calibrate_current_slope(self, event, **kw):
        result = self._run('slope', kw)
        if isinstance(result, Exception):
            raise result
        self._slope_stored = result
        return result

    def calibrate_enczero(self, event, **kw):
        return self._run('enczero', kw)

    def calibrate_itiming(self, event, **kw):
        return self._run('settling', kw)

    def fold_baseline_offset(self, event, **kw):
        return self._run('fold', kw)


class TestCalibrate(unittest.TestCase):

    def setUp(self):
        FakeCalibrate.results = {}
        FakeCalibrate.created = self.created = []
        patcher = mock.patch.object(calibrate, '_mixin',
                                    return_value=FakeCalibrate)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.node = mock.MagicMock()
        self.node.id = 5

    def names(self):
        return [name for name, _kw in self.created[-1].calls]

    def test_the_full_sequence(self):
        self.assertTrue(calibrate.calibrate_all(self.node))
        self.assertEqual(self.names(), ['test_encoder', 'ibias', 'gain',
                                        'slope', 'enczero', 'fold'])
        self.assertTrue(all(kw.get('calAll') for _n, kw in self.created[-1].calls))

    def test_the_slope_is_cleared_first(self):
        calibrate.calibrate_all(self.node)
        saves = [c.args for c in self.node.sdo.__getitem__.call_args_list]
        self.assertIn((0x3008,), saves)
        self.assertIn((0x3009,), saves)

    def test_quick_passes_quick(self):
        calibrate.calibrate_all(self.node, quick=True)
        kw = dict(self.created[-1].calls)
        self.assertTrue(kw['ibias']['quick'])
        self.assertTrue(kw['slope']['quick'])
        self.assertTrue(kw['enczero']['quick'])
        self.assertNotIn('quick', kw['gain'])

    def test_a_failed_step_stops_the_sequence(self):
        FakeCalibrate.results = {'gain': False}
        self.assertFalse(calibrate.calibrate_all(self.node))
        self.assertEqual(self.names(), ['test_encoder', 'ibias', 'gain'])

    def test_a_slope_failure_is_retried_then_skipped(self):
        FakeCalibrate.results = {'slope': RuntimeError('SYNC')}
        rep = RecordingReporter()
        self.assertTrue(calibrate.calibrate_all(self.node, reporter=rep))
        self.assertEqual(self.names().count('slope'), 2)
        self.assertNotIn('fold', self.names())          # no trustworthy fit
        self.assertEqual(len(rep.texts('warn')), 2)

    def test_enczero_abort(self):
        FakeCalibrate.results = {'enczero': False}
        self.assertFalse(calibrate.calibrate_all(self.node))

    def test_single_steps(self):
        self.assertTrue(calibrate.run_step('settling', self.node))
        self.assertEqual(self.names(), ['settling'])
        FakeCalibrate.results = {'slope': False}
        self.assertFalse(calibrate.run_step('slope', self.node))
        with self.assertRaises(calibrate.CalibrationFailed):
            calibrate.run_step('bogus', self.node)

    def test_prompts_go_to_the_reporter(self):
        rep = RecordingReporter(answers=[True])
        cal = calibrate.make_calibrator(self.node, reporter=rep)
        self.assertTrue(cal._prompt('Bias', 'high'))
        cal._prompt_ok('Fault', 'bus low')
        cal.frame_statusbar.SetStatusText('Calibrating...')
        self.assertIn('Fault: bus low', rep.texts('warn'))
        self.assertIn('Calibrating...', rep.texts('status'))



class TestRealMixin(unittest.TestCase):

    def test_the_real_mixin_accepts_the_adapter(self):
        """calibrate_menu imports (wx, no display) and binds to a node."""
        node = mock.MagicMock()
        node.id = 5
        try:
            cal = calibrate.make_calibrator(node)
        except calibrate.CalibrationFailed as exc:     # no wxPython
            self.skipTest(str(exc))
        self.assertEqual(cal.getID(), 5)
        self.assertIs(cal.network, node.network)
        for step in calibrate.STEPS:
            self.assertTrue(callable(calibrate.STEPS[step][1]))
        self.assertTrue(hasattr(cal, 'fold_baseline_offset'))

    def test_yield_is_a_no_op_without_an_app(self):
        try:
            from puckutility.gui import calibrate_menu
        except ImportError as exc:
            self.skipTest(str(exc))
        calibrate_menu._yield()
        calibrate_menu._sleep_responsive(0.01)


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
