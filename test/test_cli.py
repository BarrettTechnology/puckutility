"""``puckutility <command>`` end to end over a fake session."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from p4core import ops                                         # noqa: E402

from puckutility import cli                                    # noqa: E402
from puckutility.controllers import calibrate, system_config   # noqa: E402


class FakeSession:
    port = 'fake0'

    def __init__(self, found=(5,)):
        self.found_ids = list(found)
        self.node = None
        self.network = mock.Mock()
        self.nodes = {}

    def connect(self, port, fd=None):
        pass

    def scan(self):
        return self.found_ids

    def add_node(self, node_id, eds=None):
        node = self.nodes.setdefault(node_id, mock.MagicMock())
        node.id = node_id
        return node

    def disconnect(self):
        pass


class CliTest(unittest.TestCase):

    def run_cli(self, *argv, session=None):
        self.session = session or FakeSession()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cli.main(cli.build_parser(), list(argv),
                                session_factory=lambda: self.session,
                                failures=(calibrate.CalibrationFailed,
                                          system_config.SystemConfigError))
        return code, out.getvalue(), err.getvalue()


INFO = dict(node_id=5, firmware='4.4.0', product_code=1, model='P4-42',
            serial=7, pwm_hz=60000, bus_voltage_v=24.0, temperature_c=30,
            flashloader='2.1.0')


class TestInfo(CliTest):

    def test_reads_the_flashloader_by_default(self):
        with mock.patch.object(ops, 'info', return_value=dict(INFO)) as info:
            code, out, _err = self.run_cli('info')
        self.assertEqual(code, 0)
        self.assertTrue(info.call_args.kwargs['flashloader'])
        self.assertIn('Flashloader:  2.1.0', out)
        self.assertIn('ADC settling', out)
        self.assertIn('Enc comp', out)

    def test_no_flashloader_and_json(self):
        with mock.patch.object(ops, 'info', return_value=dict(INFO)) as info:
            code, out, _err = self.run_cli('--json', 'info', '--no-flashloader')
        self.assertEqual(code, 0)
        self.assertFalse(info.call_args.kwargs['flashloader'])
        self.assertIn('settling_ns', json.loads(out))

    def test_all(self):
        with mock.patch.object(ops, 'info',
                               side_effect=lambda s, n, **kw: dict(INFO, node_id=n)):
            code, out, _err = self.run_cli('--json', 'info', '--all',
                                           session=FakeSession([1, 2]))
        self.assertEqual([r['node_id'] for r in json.loads(out)], [1, 2])


class TestCalibrate(CliTest):

    def test_full_sequence_after_confirmation(self):
        with mock.patch.object(calibrate, 'calibrate_all',
                               return_value=True) as full:
            code, _out, _err = self.run_cli('-y', 'calibrate', '--quick')
        self.assertEqual(code, 0)
        self.assertTrue(full.call_args.kwargs['quick'])
        self.assertIs(full.call_args.args[1], self.session.network)

    def test_steps(self):
        with mock.patch.object(calibrate, 'run_step',
                               return_value=True) as step:
            code, _out, _err = self.run_cli('-y', 'calibrate', 'settling',
                                            'slope')
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0] for c in step.call_args_list],
                         ['settling', 'slope'])

    def test_a_failure_is_exit_1(self):
        with mock.patch.object(calibrate, 'calibrate_all', return_value=False):
            code, _out, _err = self.run_cli('-y', 'calibrate')
        self.assertEqual(code, 1)

    def test_usage_errors(self):
        self.assertEqual(self.run_cli('-y', 'calibrate', 'bogus')[0], 2)
        self.assertEqual(self.run_cli('-y', 'calibrate', 'slope', '--quick')[0], 2)

    def test_all(self):
        with mock.patch.object(calibrate, 'calibrate_all',
                               return_value=True) as full:
            self.run_cli('-y', 'calibrate', '--all',
                         session=FakeSession([3, 4]))
        self.assertEqual([c.args[0].id for c in full.call_args_list], [3, 4])


class TestOtherCommands(CliTest):

    def test_system_config(self):
        with mock.patch.object(system_config, 'apply',
                               return_value={1: 'configured', 2: 'missing'}):
            code, out, _err = self.run_cli('system-config', 's.ini')
        self.assertEqual(code, 0)
        self.assertIn('node 2: missing', out)
        with mock.patch.object(system_config, 'apply',
                               return_value={1: 'flash failed'}):
            self.assertEqual(self.run_cli('system-config', 's.ini')[0], 1)

    def test_flash_canable_needs_no_bus(self):
        from puckutility.controllers import canable
        with mock.patch.object(canable, 'flash_canable',
                               return_value=True) as flash:
            code, _out, _err = self.run_cli('flash-canable', session=mock.Mock(
                connect=mock.Mock(side_effect=AssertionError('no bus'))))
        self.assertEqual(code, 0)
        flash.assert_called_once_with(None, verbose=False)

    def test_the_shared_commands_are_there(self):
        parser = cli.build_parser()
        text = parser.format_help()
        for command in ('scan', 'flash', 'config', 'set-id', 'reset', 'info',
                        'calibrate', 'system-config', 'flash-canable'):
            self.assertIn(command, text)


class TestSources(unittest.TestCase):

    def test_everything_compiles(self):
        for folder in ('puckutility', 'scripts', 'test'):
            for base, _dirs, files in os.walk(os.path.join(ROOT, folder)):
                for name in files:
                    if name.endswith('.py'):
                        path = os.path.join(base, name)
                        with open(path, encoding='utf-8') as f:
                            compile(f.read(), path, 'exec')

    def test_the_gui_imports(self):
        try:
            import wx  # noqa: F401
        except ImportError as exc:
            self.skipTest(str(exc))
        from puckutility.gui import app
        self.assertTrue(os.path.isfile(app.EDS))
        self.assertTrue(callable(app.main))

    def test_package_data_is_found(self):
        from puckutility import paths
        from puckutility.controllers import canable
        self.assertTrue(os.path.isfile(paths.image('Splash.png')))
        self.assertTrue(os.path.isfile(canable._BUNDLED_FW))


if __name__ == '__main__':
    unittest.main()
