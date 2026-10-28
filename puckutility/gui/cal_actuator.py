"""cal_actuator.py — the Actuator Calibration menu: the whole calibration on
the active puck or on every puck, and its numbered steps one at a time.

A manufacturing batch shares one configuration CSV, loaded first (the
config gate); the actuator calibration is what the CSV cannot carry,
measured on each unit (p4core.actuator.plans.ACTUATOR).  A mixin of the
calibrate menu (calibrate_menu.calibrate inherits it).  The steps are
p4core's, through controllers.calibrate; this keeps the frame side: the gate and the
confirmations, the progress gauge, the ADC monitor, the reboot an encoder
filter change needs, and the question whether to Save.All afterwards.
Nothing is saved to NV unless the user says so.

Each step item runs that step alone in this puck's cached Session, so fold
finds the slope sweep a Slope run left in it.  Bias and gain are p4core
steps like the rest: puckutility keeps no current-sense calibration of its
own.  Only the steps the puck's firmware can run are offered: the Steps
menu lists those (app._apply_firmware_gates) and Active Puck runs those.

Everything runs on the GUI thread, as the calibrate menu always has, with
the window disabled; the reporter and the drive's pump keep it painting.
"""
import time

import wx

from p4core.cia402 import MODE_IDLE
from p4core.actuator import plans
from ..controllers import calibrate as actuator
from .cal_common import _sleep_responsive, _yield, GuiCalReporter

#: How long a rebooted drive has to answer again (s).
_REBOOT_TIMEOUT_S = 15.0


def _pump():
    """The drive's pump (P4Drive *pump*): called at every abort check and
    through its settling waits, it keeps the window painting.  Only on the
    GUI thread: a step may get there from a PDO callback."""
    if wx.GetApp() is not None and wx.IsMainThread():
        wx.YieldIfNeeded()


class _StepReporter(GuiCalReporter):
    """GuiCalReporter whose progress drives the gauge (_upd)."""

    def __init__(self, frame, upd):
        super().__init__(frame)
        self.upd = upd

    def progress(self, pct, text=None):
        self.upd(int(pct))
        if text:
            self.status(text)
        else:
            _yield()


def _numbered_list(node, keys):
    """*keys* with the numbers the Steps menu gives them on *node* (its
    firmware's steps, in run order)."""
    return '\n'.join('{:2d}. {}'.format(n, title)
                     for n, key, title in actuator.numbered(
                         actuator.supported(node))
                     if key in keys)


class ActuatorCal:
    # -- the menu items ----------------------------------------------------
    def calibrate_actuator(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Actuator Calibration > Active Puck: every step the active puck's
        firmware can run, then the summary and the offer to Save.All."""
        keys = self._actuator_ready()
        if not keys:
            return False
        ok, session = self._run_actuator(keys, fresh=True)
        self._actuator_done(session, keys)
        return ok

    def calibrate_actuator_all_pucks(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Actuator Calibration > All Pucks: every step on every puck, one
        after another.  Whether to Save.All each puck that passes is asked
        once, up front.  Each puck runs the steps its firmware can."""
        if not self._actuator_ready(every_puck=True):
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
            ok, session = self._run_actuator(actuator.supported(self.node),
                                             fresh=True)
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

    def calibrate_step_bias(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('bias')

    def calibrate_step_gain(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        return self._actuator_step('gain')

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

    # -- the frame side ----------------------------------------------------
    def _config_gate(self):
        """Never drive control modes for calibration while a firmware flash
        is still pending configuration (see select_test): the batch CSV sets
        the current-sense scaling and limits the calibration relies on, and
        is stage 0 of the actuator calibration.  Offers to load it now."""
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            if self._prompt('Configuration Required',
                            "Configuration is required after a firmware "
                            "update.\nApply configuration before calibrating."
                            "\n\nWould you like to configure the active Puck "
                            "now?"):
                self.file_to_p4(None)
            return False
        return True

    def _adc_quiet(self):
        """Quiet the ADC monitor: it contends for SDO reads."""
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

    def _adc_restore(self):
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

    def _actuator_step(self, key):
        """One actuator step alone, in this puck's cached session.  The
        Steps menu lists only what the firmware can run."""
        if not self._actuator_ready((key,)):
            return False
        ok, session = self._run_actuator((key,))
        self._actuator_done(session, (key,))
        return ok

    def _actuator_ready(self, keys=None, every_puck=False):
        """The config gate (the batch CSV first), a puck, and the user's
        go-ahead for *keys* (default: the steps the active puck's firmware
        can run).  Returns the keys, or () when not going ahead."""
        if not self._config_gate():
            return ()
        if self.check_for_node() == False:
            return ()
        if keys is None:
            keys = actuator.supported(self.node)
        if not keys:
            return ()
        msg = '{}:\n\n{}\n\n'.format(
            'Actuator calibration of every puck on the bus' if every_puck
            else 'Actuator calibration of puck {}'.format(self.getID()),
            _numbered_list(self.node, keys))
        msg += ('The motor turns{}: the output must be free to turn.\n\n'
                'Continue?'.format(', continuously for cogging'
                                   if 'cogging' in keys else ''))
        return tuple(keys) if self._prompt('Actuator Calibration', msg) else ()

    def _actuator_session(self, reporter, fresh=False):
        """This puck's Session, reporting to *reporter*: cached per
        node ID, so one step can use another's result; a new one when
        *fresh* or when the node object changed (a rescan)."""
        cache = self.__dict__.setdefault('_actuator_sessions', {})
        node_id = self.getID()
        session = cache.get(node_id)
        if fresh or session is None or session.dev.node is not self.node:
            session = actuator.make_session(self.node, reporter,
                                            reboot=self._actuator_reboot,
                                            sync=self.sync_owner)
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
        self._ui_lock()
        self._adc_quiet()
        self.OnStartTask(None)
        reporter = _StepReporter(self, self.UpdateUI)
        t0 = time.time()
        try:
            ok, session = actuator.calibrate_actuator(
                self.node, reporter, steps=keys,
                session=self._actuator_session(reporter, fresh))
            if ok and set(actuator.supported(self.node)) <= set(keys):
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
            # SYNC off whatever a step left running; the ADC monitor (if it
            # was on) starts its own again.
            self.sync_owner.set_sync_rate(0)
            self._adc_restore()
            self.OnTaskComplete()
            print('Actuator calibration finished in {:.1f} s.'.format(
                time.time() - t0))
            self._ui_unlock()

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
        self._ui_lock()
        try:
            ok = actuator.save_results(session,
                                       _StepReporter(self, self.UpdateUI))
        except Exception as exc:
            self._cal_fault(exc)
            return False
        finally:
            self._ui_unlock()
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
        """The session's reboot: NMT
        reset, wait for the boot-up and for the application to answer (the
        flashloader answers SDO first, but not 0x6060), then select the node
        again so the app's PDO maps and state follow.  Returns the node."""
        node_id = self.getID()
        print('Rebooting node {} ...'.format(node_id))
        self.sync_owner.set_sync_rate(0)
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
