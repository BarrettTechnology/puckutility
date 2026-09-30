"""The entry point: GUI vs command line, and the old flat flags."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from puckutility import main                                  # noqa: E402


class TestLegacyFlags(unittest.TestCase):

    def t(self, argv):
        return main.translate_legacy(argv.split())

    def test_scan_ignores_ids(self):
        self.assertEqual(self.t('--can can0 --scan'), [['--can', 'can0', 'scan']])

    def test_info_without_ids_shows_every_puck(self):
        self.assertEqual(self.t('--info'), [['info', '--all']])
        self.assertEqual(self.t('--info --id 4'), [['--id', '4', 'info']])

    def test_several_ids_run_one_after_another(self):
        self.assertEqual(self.t('--can can1 --id 1 2 --flash fw.bin'),
                         [['--can', 'can1', '--id', '1', 'flash', 'fw.bin'],
                          ['--can', 'can1', '--id', '2', 'flash', 'fw.bin']])

    def test_config_is_config_load(self):
        self.assertEqual(self.t('--all --config m.csv'),
                         [['config', 'load', 'm.csv', '--all']])

    def test_calibrations(self):
        self.assertEqual(self.t('--all --calibrate'), [['calibrate', '--all']])
        self.assertEqual(self.t('--id 3 --calibrate --quick'),
                         [['--id', '3', 'calibrate', '--quick']])
        self.assertEqual(self.t('--id 3 --calibrate-settling'),
                         [['--id', '3', 'calibrate', 'settling']])
        self.assertEqual(self.t('--id 3 --calibrate-slope'),
                         [['--id', '3', 'calibrate', 'slope']])

    def test_system_config(self):
        self.assertEqual(self.t('--can can0 --system-config s.ini'),
                         [['--can', 'can0', 'system-config', 's.ini']])

    def test_set_id_needs_exactly_one_current_id(self):
        self.assertEqual(self.t('--id 5 --set-id 7'),
                         [['--id', '5', 'set-id', '7']])
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit):
            self.t('--id 5 6 --set-id 7')

    def test_flash_canable(self):
        self.assertEqual(self.t('--flash-canable'), [['flash-canable']])
        self.assertEqual(self.t('--verbose --flash-canable x.bin'),
                         [['--verbose', 'flash-canable', 'x.bin']])

    def test_two_operations_are_refused(self):
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit):
            self.t('--scan --info')


class TestRouting(unittest.TestCase):

    def route(self, argv):
        with mock.patch.object(main, 'gui_main', return_value=0) as gui, \
                mock.patch('puckutility.cli.main', return_value=0) as cli:
            main.main(argv)
        return gui, cli

    def test_no_arguments_or_gui_flags_start_the_gui(self):
        for argv in ([], ['--touchscreen'], ['--can', 'vcan0'],
                     ['--can=can1', '--touchscreen']):
            gui, cli = self.route(argv)
            gui.assert_called_once_with(argv)
            cli.assert_not_called()

    def test_gui_command(self):
        gui, _cli = self.route(['gui', '--touchscreen'])
        gui.assert_called_once_with(['--touchscreen'])

    def test_a_command_goes_to_the_cli(self):
        gui, cli = self.route(['--can', 'can0', 'scan'])
        gui.assert_not_called()
        cli.assert_called_once_with(['--can', 'can0', 'scan'])

    def test_old_flags_are_translated(self):
        _gui, cli = self.route(['--can', 'can0', '--id', '1', '2',
                                '--calibrate'])
        self.assertEqual([c.args[0] for c in cli.call_args_list],
                         [['--can', 'can0', '--id', '1', 'calibrate'],
                          ['--can', 'can0', '--id', '2', 'calibrate']])

    def test_the_worst_exit_code_wins(self):
        with mock.patch('puckutility.cli.main', side_effect=[0, 3, 1]):
            self.assertEqual(main.main(['--id', '1', '2', '3', '--info']), 3)


if __name__ == '__main__':
    unittest.main()
