"""The calibrate menu's board steps are thin wrappers over p4core.calibrate:
the right settings go through, a declined step answers False, a failure
propagates inside Calibrate All and goes to _cal_fault from a menu item."""

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
    def __init__(self):
        self.node = mock.MagicMock()
        self.ADC_ON = False
        self.adcWasON = False
        self.choice_test = mock.MagicMock()
        self.choice_test.GetSelection.return_value = 0
        self.frame_statusbar = mock.MagicMock()
        self.faults, self.prompts = [], []
        self.prompt_answer = True

    def check_for_node(self):
        return True

    def _menu_idle_takeover(self):
        pass

    def Disable(self):
        pass

    def Enable(self):
        pass

    def OnStartTask(self, event):
        pass

    def OnTaskComplete(self):
        pass

    def UpdateUI(self, value):
        pass

    def on_off_adc(self, event):
        pass

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
            self.assertTrue(f.calibrate_ibias(None, calAll=True, quick=True))
        self.assertEqual(fn.call_args.kwargs, calibrate.BIAS_MENU_QUICK)
        with mock.patch.object(calibrate, 'calibrate_bias') as fn:
            f.calibrate_ibias(None)
        self.assertEqual(fn.call_args.kwargs, calibrate.BIAS_MENU)

    def test_gain_declined_is_false(self):
        f = Frame()
        with mock.patch.object(calibrate, 'calibrate_gain',
                               side_effect=calibrate.CalibrationDeclined('no')) as fn:
            self.assertFalse(f.calibrate_igainfactor(None, calAll=True))
        self.assertEqual(fn.call_args.kwargs, calibrate.GAIN_MENU)

    def test_failure_raises_in_sequence_faults_from_menu(self):
        f = Frame()
        err = calibrate.CalibrationError('no current')
        with mock.patch.object(calibrate, 'calibrate_gain', side_effect=err):
            with self.assertRaises(calibrate.CalibrationError):
                f.calibrate_igainfactor(None, calAll=True)
            f.calibrate_igainfactor(None)
        self.assertEqual(f.faults, [err])

    def test_ezero_failure_asks_to_continue(self):
        f = Frame()
        f.prompt_answer = False
        with mock.patch.object(calibrate, 'calibrate_ezero',
                               side_effect=calibrate.CalibrationError('stalled')) as fn:
            self.assertFalse(f.calibrate_enczero(None, calAll=True, quick=True))
        self.assertEqual(fn.call_args.kwargs, dict(steps=calibrate.EZERO_QUICK_STEPS))
        self.assertIn('stalled', f.prompts[0])

    def test_progress_drives_the_gauge(self):
        f = Frame()
        seen = []

        def step(node, reporter, **kw):
            reporter.progress(40)
        with mock.patch.object(calibrate, 'calibrate_bias', side_effect=step):
            f.calibrate_ibias(None, calAll=True, _upd=seen.append)
        self.assertEqual(seen, [40])


if __name__ == '__main__':
    unittest.main()
