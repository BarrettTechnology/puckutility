"""cal_actuator.py — the Actuator Calibration menu: the whole calibration on
the active puck or on every puck, and its numbered steps one at a time.

A manufacturing batch shares one configuration CSV, loaded first (the
config gate); the actuator calibration is what the CSV cannot carry,
measured on each unit (p4sysid.plans.ACTUATOR).  A mixin of the calibrate
menu (calibrate_menu.calibrate inherits it).  The steps are p4sysid's,
through controllers.calibrate; this keeps the frame side: the gate and the
confirmations, the progress gauge, the ADC monitor, the reboot an encoder
filter change needs, and the question whether to Save.All afterwards.
Nothing is saved to NV unless the user says so.

Steps 2 and 3, current-sense bias and gain, are cal_board's own menu
handlers (calibrate_ibias, calibrate_igainfactor); the rest run alone in
this puck's cached p4sysid Session, so fold finds the slope sweep a
Slope run left in it.

Everything runs on the GUI thread, as the calibrate menu always has, with
the window disabled; the reporter and p4sysid's pump keep it painting.
"""
import time

import wx

from p4core.cia402 import MODE_IDLE
from p4sysid import plans
from ..controllers import calibrate as actuator
from .cal_board import _StepReporter
from .cal_common import _sleep_responsive

#: How long a rebooted drive has to answer again (s).
_REBOOT_TIMEOUT_S = 15.0


def _pump():
    """p4sysid's pump (P4Sysid *pump*): called at every abort check and
    through its settling waits, it keeps the window painting.  Only on the
    GUI thread: p4sysid may get there from a PDO callback."""
    if wx.GetApp() is not None and wx.IsMainThread():
        wx.YieldIfNeeded()


def _numbered_list(keys):
    """*keys* as the Steps menu numbers them."""
    return '\n'.join('{:2d}. {}'.format(n, st.title)
                     for n, _stage, st in plans.numbered(plans.ACTUATOR)
                     if st.key in keys)


class ActuatorCal:
    # -- the menu items ----------------------------------------------------
    def calibrate_actuator(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Actuator Calibration > Active Puck: every step on the active
        puck, then the summary and the offer to Save.All."""
        if not self._actuator_ready(plans.ACTUATOR):
            return False
        ok, session = self._run_actuator(plans.ACTUATOR, fresh=True)
        self._actuator_done(session, plans.ACTUATOR)
        return ok

    def calibrate_actuator_all_pucks(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Actuator Calibration > All Pucks: every step on every puck, one
        after another.  Whether to Save.All each puck that passes is asked
        once, up front."""
        if not self._actuator_ready(plans.ACTUATOR, every_puck=True):
            return False
        save = self._prompt('Save to NV?',
                            "Save each puck's results to NV (Save.All) when "
                            "its calibration passes?\n\nNo leaves them in RAM "
                            "(lost at the next reboot).")
        starting_id = self.getID()
        lines, all_ok = [], True
        for node_id in list(self.network.scanner.nodes):
            self.choice_id.SetSelection(self.network.scanner.nodes.index(node_id))
            self.select_id(None)
            print('--- Actuator calibration, node {} ---'.format(node_id))
            ok, session = self._run_actuator(plans.ACTUATOR, fresh=True)
            verdict = 'PASS' if ok else 'FAILED'
            if ok and save and session is not None and session.unsaved:
                ok = self._actuator_save(session)
                verdict = 'PASS, saved' if ok else 'PASS, Save.All FAILED'
            elif ok and session is not None and session.unsaved:
                verdict = 'PASS, not saved'
            all_ok = all_ok and ok
            lines.append('Node {}: {}'.format(node_id, verdict))
            if session is not None:
                print(actuator.summary_text(session))
        if starting_id in self.network.scanner.nodes:
            self.choice_id.SetSelection(
                self.network.scanner.nodes.index(starting_id))
            self.select_id(None)
        self._actuator_message('Actuator Calibration', '\n'.join(lines))
        return all_ok

    def calibrate_step_encoder(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('encoder')

    def calibrate_step_slope(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('slope')

    def calibrate_step_fold(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('fold')

    def calibrate_step_winding(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('winding')

    def calibrate_step_runout(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('runout')

    def calibrate_step_cogging(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('cogging')

    def calibrate_step_velocity_refine(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('velocity_refine')

    def calibrate_step_position_refine(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('position_refine')

    # -- the frame side ----------------------------------------------------
    def _actuator_step(self, key):
        """One actuator step alone, in this puck's cached session."""
        if not self._actuator_ready((key,)):
            return False
        ok, session = self._run_actuator((key,))
        self._actuator_done(session, (key,))
        return ok

    def _actuator_ready(self, keys, every_puck=False):
        """The config gate (the batch CSV first), a puck, and the user's
        go-ahead for *keys*."""
        if not self._config_gate():
            return False
        if self.check_for_node() == False:
            return False
        moving = [k for k in keys if k in ('cogging', 'velocity_refine',
                                           'position_refine')]
        msg = '{}:\n\n{}\n\n'.format(
            'Actuator calibration of every puck on the bus' if every_puck
            else 'Actuator calibration of puck {}'.format(self.getID()),
            _numbered_list(keys))
        msg += ('The motor turns{}: the output must be free to turn.\n\n'
                'Continue?'.format(', continuously for cogging and the refine '
                                   'steps' if moving else ''))
        return bool(self._prompt('Actuator Calibration', msg))

    def _actuator_session(self, reporter, fresh=False):
        """This puck's p4sysid Session, reporting to *reporter*: cached per
        node ID, so one step can use another's result; a new one when
        *fresh* or when the node object changed (a rescan)."""
        cache = self.__dict__.setdefault('_actuator_sessions', {})
        node_id = self.getID()
        session = cache.get(node_id)
        if fresh or session is None or session.dev.node is not self.node:
            session = actuator.make_session(self.node, reporter,
                                            reboot=self._actuator_reboot)
            cache[node_id] = session
        else:
            # A cached session still logs to the run that made it.
            session.dev._log = session.log = reporter.note
            session.dev._should_abort = reporter.should_abort
        session.dev._pump = _pump
        return session

    def _run_actuator(self, keys, fresh=False):
        """*keys* on the active puck with the window disabled and the gauge
        running.  Returns (ok, session); session None when it could not
        start."""
        self._menu_idle_takeover()
        self.Disable()
        self._adc_quiet()
        self.OnStartTask(None)
        reporter = _StepReporter(self, self.UpdateUI)
        t0 = time.time()
        try:
            ok, session = actuator.calibrate_actuator(
                self.node, reporter, steps=keys,
                session=self._actuator_session(reporter, fresh))
            if ok and set(plans.ACTUATOR) <= set(keys):
                self.requireCal = False
            return ok, session
        except Exception as exc:
            self._cal_fault(exc)
            return False, None
        finally:
            try:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.choice_test.SetSelection(0)
                self.lastMode = 0
            except Exception:
                pass
            self._adc_restore()
            self.OnTaskComplete()
            print('Actuator calibration finished in {:.1f} s.'.format(
                time.time() - t0))
            self.Enable()

    def _actuator_done(self, session, keys):
        """The summary, then Save.All if the user wants it."""
        if session is None:
            return
        text = actuator.summary_text(session, keys)
        print(text)
        if not session.unsaved:
            self._actuator_message('Actuator Calibration', text)
            return
        if self._prompt('Actuator Calibration',
                        text + '\n\nThe results are in RAM only.\n'
                        'Save to NV (Save.All)?'):
            self._actuator_save(session)

    def _actuator_save(self, session):
        self.Disable()
        try:
            ok = actuator.save_results(session,
                                       _StepReporter(self, self.UpdateUI))
        except Exception as exc:
            self._cal_fault(exc)
            return False
        finally:
            self.Enable()
        print('Save.All {}'.format('done' if ok else 'FAILED'))
        if not ok:
            self._prompt_ok('Save to NV', 'Save.All failed:\n\n' +
                            plans.summary_text(session, ('save',)))
        return ok

    def _actuator_message(self, title, text):
        dlg = wx.MessageDialog(None, text, title, wx.OK | wx.ICON_INFORMATION)
        dlg.ShowModal()
        dlg.Destroy()

    def _actuator_reboot(self):
        """p4sysid's reboot (modelled on p4sysid.__main__.make_reboot): NMT
        reset, wait for the boot-up and for the application to answer (the
        flashloader answers SDO first, but not 0x6060), then select the node
        again so the app's PDO maps and state follow.  Returns the node."""
        node_id = self.getID()
        print('Rebooting node {} ...'.format(node_id))
        try:
            self.network.sync.stop()
        except Exception:
            pass
        old = self.node
        old.nmt.send_command(0x81)
        try:
            old.nmt.wait_for_bootup(10)
        except Exception:
            _sleep_responsive(3)
        _sleep_responsive(0.5)
        deadline = time.monotonic() + _REBOOT_TIMEOUT_S
        while True:
            try:
                old.sdo["SetModeOfOperation"].raw
                break
            except Exception as exc:     # the flashloader aborts it
                if time.monotonic() > deadline:
                    raise ConnectionError('node {} did not come back after a '
                                          'reset: {}'.format(node_id, exc))
                _sleep_responsive(0.5)
        self.choice_id.SetSelection(self.network.scanner.nodes.index(node_id))
        self.select_id(None)
        return self.node
