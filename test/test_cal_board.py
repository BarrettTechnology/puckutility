"""The calibrate menu's current-sense bias and gain items (actuator steps 2
and 3) are thin wrappers over p4core.calibrate: the right settings go
through, a declined step answers False, a failure goes to _cal_fault; and
the config gate in front of every calibration."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from p4core import calibrate  # noqa: E402

try:
    from puckutility.gui.cal_board import BoardCal
except ImportError as exc:     # no wxPython
    BoardCal = None
    SKIP = str(exc)


class Frame(BoardCal if BoardCal else object):
    """The frame side the calibrate mixins use, recorded."""

    def __init__(self):
        self.node = mock.MagicMock()
        self.ADC_ON = False
        self.adcWasON = False
        self.requireConfig = False
        self.choice_test = mock.MagicMock()
        self.choice_test.GetSelection.return_value = 0
        self.frame_statusbar = mock.MagicMock()
        self.faults, self.prompts, self.gauge = [], [], []
        self.prompt_answer = True
        self.enabled = True
        self.configured = 0

    def check_for_node(self):
        return True

    def _menu_idle_takeover(self):
        pass

    def Disable(self):
        self.enabled = False

    def Enable(self):
        self.enabled = True

    def OnStartTask(self, event):
        pass

    def OnTaskComplete(self):
        pass

    def UpdateUI(self, value):
        self.gauge.append(value)

    def on_off_adc(self, event):
        pass

    def file_to_p4(self, event):
        self.configured += 1

    def _cal_fault(self, exc):
        self.faults.append(exc)

    def _prompt(self, title, msg):
        self.prompts.append(msg)
        return self.prompt_answer


@unittest.skipIf(BoardCal is None, 'needs wxPython')
class TestBoardWrappers(unittest.TestCase):
    def test_bias_settings(self):
        f = Frame()
        with mock.patch.object(calibrate, 'calibrate_bias') as fn:
            self.assertTrue(f.calibrate_ibias(None))
        self.assertEqual(fn.call_args.kwargs, calibrate.BIAS_MENU)
        self.assertIs(fn.call_args.args[0], f.node)

    def test_gain_declined_is_false(self):
        f = Frame()
        with mock.patch.object(calibrate, 'calibrate_gain',
                               side_effect=calibrate.CalibrationDeclined('no')) as fn:
            self.assertFalse(f.calibrate_igainfactor(None))
        self.assertEqual(fn.call_args.kwargs, calibrate.GAIN_MENU)
        self.assertEqual(f.faults, [])

    def test_a_failure_goes_to_the_fault_dialog(self):
        f = Frame()
        err = calibrate.CalibrationError('no current')
        with mock.patch.object(calibrate, 'calibrate_gain', side_effect=err):
            self.assertFalse(f.calibrate_igainfactor(None))
        self.assertEqual(f.faults, [err])
        self.assertTrue(f.enabled)

    def test_progress_drives_the_gauge(self):
        f = Frame()

        def step(node, reporter, **kw):
            reporter.progress(40)
        with mock.patch.object(calibrate, 'calibrate_bias', side_effect=step):
            f.calibrate_ibias(None)
        self.assertEqual(f.gauge, [40])

    def test_the_config_gate(self):
        f = Frame()
        f.requireConfig = True
        f.prompt_answer = False
        self.assertFalse(f._config_gate())
        self.assertIn('Configuration is required', f.prompts[0])
        self.assertEqual(f.configured, 0)
        f.prompt_answer = True
        self.assertFalse(f._config_gate())
        self.assertEqual(f.configured, 1)
        f.requireConfig = False
        self.assertTrue(f._config_gate())


if __name__ == '__main__':
    unittest.main()
