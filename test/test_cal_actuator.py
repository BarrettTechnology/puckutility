"""The Actuator Calibration menu's handlers over a fake frame and a fake
session: the confirmation, the plan's keys (those the firmware can run),
one cached session per puck for the single steps, Save.All only when the
user says so, All Pucks, the reboot an encoder filter change needs, and the
config gate in front of every calibration.  Bias and gain are p4core steps
like the rest."""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..')))
sys.path.insert(0, HERE)

from p4core.actuator import plans  # noqa: E402
from p4core.firmware import FEATURES, Firmware  # noqa: E402

CURRENT = Firmware((4, 5, 0), '4.5.0', frozenset(FEATURES) - {'legacy_polarity'})
LEGACY = Firmware((4, 3, 5), '4.3.5', frozenset({'legacy_polarity'}))

try:
    from puckutility.gui import cal_actuator
    from puckutility.gui.cal_actuator import ActuatorCal
except ImportError:            # no wxPython
    ActuatorCal = None


class Frame:
    """The frame side the calibrate mixin uses, recorded."""

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
        self.sync_owner = mock.Mock()

    def check_for_node(self):
        return True

    def _menu_idle_takeover(self):
        pass

    def _ui_lock(self):
        self.enabled = False

    def _ui_unlock(self):
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


class FakeSession:
    """A steps.Session for plans.run/save: passes every step
    (unless told to fail one) and leaves the results unsaved."""

    def __init__(self, node, fail=()):
        self.dev = mock.Mock(node=node)
        self.log = None
        self.runs = []
        self.results = {}
        self.unsaved = False
        self.fail = set(fail)

    def run(self, keys, on_update=None):
        self.runs.append(list(keys))
        ok = True
        for k in keys:
            if not ok:
                status = 'skipped'
            elif k in self.fail:
                status, ok = 'fail', False
            else:
                status = 'pass'
                self.unsaved = k != 'save'
            self.results[k] = {'status': status, 'detail': ''}
            on_update(k)
        return ok


if ActuatorCal is not None:
    class AFrame(ActuatorCal, Frame):
        def __init__(self, nodes=(5,)):
            super().__init__()
            self.network = mock.Mock()
            self.network.scanner.nodes = list(nodes)
            self.ID = nodes[0]
            self.nodes = {n: mock.MagicMock(name='node%d' % n,
                                            _p4_firmware=CURRENT)
                          for n in nodes}
            self.node = self.nodes[self.ID]
            self.choice_id = mock.Mock()
            self.messages = []
            self.requireCal = True
            self.selected = []

        def getID(self):
            return self.ID

        def select_id(self, event):
            self.ID = self.network.scanner.nodes[
                self.choice_id.SetSelection.call_args.args[0]]
            self.node = self.nodes[self.ID]
            self.selected.append(self.ID)

        def _actuator_message(self, title, text):
            self.messages.append(text)

        def _prompt_ok(self, title, msg):
            self.messages.append(msg)


@unittest.skipIf(ActuatorCal is None, 'needs wxPython')
class TestActuatorMenu(unittest.TestCase):

    def setUp(self):
        self.sessions = []
        self.fail_keys = ()

        def make(node, reporter=None, reboot=None, sync=None):
            s = FakeSession(node, self.fail_keys)
            s.reboot = reboot
            s.sync = sync
            self.sessions.append(s)
            return s
        p = mock.patch.object(cal_actuator.actuator.plans, 'make_session',
                              side_effect=make)
        p.start()
        self.addCleanup(p.stop)

    def test_the_whole_calibration_asks_runs_the_plan_and_asks_to_save(self):
        f = AFrame()
        f.answers = iter([True, False])           # go; don't save
        f._prompt = lambda t, m: f.prompts.append(m) or next(f.answers)
        self.assertTrue(f.calibrate_actuator(None))
        go, save = f.prompts
        for n, key in enumerate(plans.run_order(plans.ACTUATOR), 1):
            self.assertIn('{:2d}. {}'.format(n, plans.title(key)), go)
        self.assertIn('continuously', go)
        self.assertIn('free to turn', go)
        s, = self.sessions
        self.assertEqual(s.runs, [plans.run_order(plans.ACTUATOR)])
        self.assertIs(s.dev.node, f.node)
        self.assertEqual(s.reboot, f._actuator_reboot)
        # The steps' streams go through the frame's SYNC owner, and the
        # calibration leaves the SYNC off (the ADC monitor restarts its own).
        self.assertIs(s.sync, f.sync_owner)
        f.sync_owner.set_sync_rate.assert_called_with(0)
        self.assertIn('Save to NV (Save.All)?', save)
        self.assertEqual(len(s.runs), 1)          # declined: no Save.All
        self.assertFalse(f.requireCal)
        self.assertTrue(f.enabled)

    def test_save_on_yes(self):
        f = AFrame()
        self.assertTrue(f.calibrate_actuator(None))
        s, = self.sessions
        self.assertEqual(s.runs[-1], ['save'])
        self.assertFalse(s.unsaved)

    def test_declined_or_gated_runs_nothing(self):
        f = AFrame()
        f.prompt_answer = False
        self.assertFalse(f.calibrate_actuator(None))
        f = AFrame()
        f.requireConfig = True                    # the batch CSV first
        f.prompt_answer = False
        self.assertFalse(f.calibrate_actuator(None))
        self.assertFalse(f.calibrate_step_runout(None))
        self.assertEqual(self.sessions, [])

    def test_a_failure_shows_the_summary_and_keeps_requirecal(self):
        self.fail_keys = ('winding',)
        f = AFrame()
        self.assertFalse(f.calibrate_actuator(None))
        self.assertTrue(f.requireCal)
        s, = self.sessions
        self.assertEqual(s.results['runout']['status'], 'skipped')
        self.assertIn('FAIL', f.prompts[-1])      # unsaved: summary + ask

    def test_legacy_firmware_offers_only_what_it_can_run(self):
        # No warning and no list of what the firmware lacks: the steps it
        # can run, numbered from 1, and only those run.
        f = AFrame()
        f.node._p4_firmware = LEGACY
        f._prompt = lambda t, m: f.prompts.append(m) or 'Save' not in m
        self.assertTrue(f.calibrate_actuator(None))
        go = f.prompts[0]
        listed = [line.strip() for line in go.splitlines()
                  if line.strip()[:1].isdigit()]
        self.assertEqual(listed, ['1. Encoder filter + rest test',
                                  '2. Current-sense bias',
                                  '3. Current-sense gain',
                                  '4. Electrical zero + winding order'])
        for word in ('skipped', 'firmware', '4.3.5', 'runout', 'cogging',
                     'continuously'):
            self.assertNotIn(word, go.lower())
        s, = self.sessions
        self.assertEqual(s.runs, [['encoder', 'bias', 'gain', 'winding']])
        self.assertFalse(f.requireCal)            # all it can run passed

    def test_all_pucks_runs_what_each_puck_can(self):
        f = AFrame(nodes=(3, 4))
        f.nodes[3]._p4_firmware = LEGACY
        f._prompt = lambda t, m: f.prompts.append(m) or 'Save' not in t
        self.assertTrue(f.calibrate_actuator_all_pucks(None))
        self.assertEqual([s.runs[0] for s in self.sessions],
                         [['encoder', 'bias', 'gain', 'winding'],
                          plans.run_order(plans.ACTUATOR)])

    def test_single_steps_share_the_pucks_session(self):
        f = AFrame()
        f.prompt_answer = True
        f._prompt = lambda t, m: f.prompts.append(m) or 'Save' not in m
        self.assertTrue(f.calibrate_step_slope(None))
        self.assertTrue(f.calibrate_step_fold(None))
        s, = self.sessions
        self.assertEqual(s.runs, [['slope'], ['fold']])
        self.assertNotIn('continuously', f.prompts[0])
        self.assertIn(' 4. Current-sense slope', f.prompts[0])
        self.assertTrue(f.calibrate_step_cogging(None))
        self.assertIn('continuously', f.prompts[-2])
        # A whole calibration starts a fresh session.
        f.calibrate_actuator(None)
        self.assertEqual(len(self.sessions), 2)
        # So does a different node object (a rescan).
        f.node = f.nodes[5] = mock.MagicMock()
        f.calibrate_step_winding(None)
        self.assertEqual(len(self.sessions), 3)

    def test_every_step_item_runs_its_key(self):
        f = AFrame()
        f._prompt = lambda t, m: 'Save' not in m      # go; don't save
        for key in plans.ACTUATOR:
            getattr(f, 'calibrate_step_' + key)(None)
            self.assertEqual(self.sessions[0].runs[-1][0], key)

    def test_all_pucks_asks_once_and_saves_each_passing_puck(self):
        f = AFrame(nodes=(3, 4))
        self.assertTrue(f.calibrate_actuator_all_pucks(None))
        self.assertEqual(len(f.prompts), 2)      # go, save-each
        self.assertEqual([s.dev.node for s in self.sessions],
                         [f.nodes[3], f.nodes[4]])
        for s in self.sessions:
            self.assertEqual(s.runs[-1], ['save'])
        self.assertEqual(f.selected[-1], 3)       # back to the starting puck
        self.assertIn('Node 4: PASS, saved', f.messages[-1])

    def test_all_pucks_without_saving(self):
        f = AFrame(nodes=(3, 4))
        f._prompt = lambda t, m: f.prompts.append(m) or 'Save' not in t
        f.calibrate_actuator_all_pucks(None)
        for s in self.sessions:
            self.assertNotIn(['save'], s.runs)
        self.assertIn('Node 3: PASS, not saved', f.messages[-1])

    def test_the_reboot(self):
        f = AFrame(nodes=(3, 4))
        f.ID, f.node = 4, f.nodes[4]
        old = f.node
        with mock.patch.object(cal_actuator, '_sleep_responsive'):
            node = f._actuator_reboot()
        f.sync_owner.set_sync_rate.assert_called_once_with(0)
        old.nmt.send_command.assert_called_once_with(0x81)
        old.nmt.wait_for_bootup.assert_called_once()
        f.choice_id.SetSelection.assert_called_with(1)
        self.assertEqual(f.selected, [4])
        self.assertIs(node, f.node)


    def test_the_config_gate(self):
        f = AFrame()
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
