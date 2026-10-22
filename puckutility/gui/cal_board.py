"""cal_board.py — Current-sense bias and gain, encoder zero and direction, and the
Calibrate All sequences.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it).
"""
import wx
import canopen
import time
import math
import struct
import threading
import webbrowser
import configparser
import platform
from p4core.cia402 import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE, MODE_PROFILE_TRQ, MODE_PROFILE_VEL,
    MODE_PROFILE_POS,
)
from canopen.sdo import SdoAbortedError
from p4core.can_backend import sdo_contention_message
from p4core import ezero
from p4core.calibrate import firmware_version
from p4core import flash as flashp4
from ..paths import _resolve_path, FIRMWARE_DIR, CONFIG_DIR
from p4core import calibrate
from p4sysid import boardcal
from .cal_common import _yield, _sleep_responsive, GuiCalReporter


class _StepReporter(GuiCalReporter):
    """GuiCalReporter whose progress drives the step's gauge (_upd)."""

    def __init__(self, frame, upd):
        super().__init__(frame)
        self.upd = upd

    def progress(self, pct, text=None):
        self.upd(int(pct))
        if text:
            self.status(text)
        else:
            _yield()


class BoardCal:
    def calibrate_all_pucks(self, event):
        # Gate: a fresh firmware flash leaves stale/default config on the puck
        # (i_peak, I_cont, current-sense scaling). Calibration drives control
        # modes off that config, so it must not run until configuration has been
        # applied since the flash -- mirrors the mode-switch gate in select_test().
        if getattr(self, 'requireConfig', False):
            msg = ("Configuration is required after a firmware update.\n"
                   "Apply configuration before calibrating.\n\n"
                   "Would you like to configure the active Puck now?")
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.file_to_p4(None)
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sweep
        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID) # Move to next ID for calibration
            self.select_id(None)

            # print("Running full calibration for Puck {}".format(self.getID()))
            self.calibrate_all(None)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID) # Return to starting ID after completion
        self.select_id(None)

    #: The gauge span each board step fills in a Calibrate All (start, width, %):
    #: gain's rotation dominates, then the slope sweep and the zero's two turns.
    _BOARD_GAUGE = {'encoder': (0, 5), 'bias': (5, 15), 'gain': (20, 52),
                    'slope': (72, 10), 'fold': (82, 3), 'enczero': (85, 15)}

    def _config_gate(self):
        """Backstop for the config gate: never drive control modes for
        calibration while a firmware flash is still pending configuration
        (see calibrate_all_pucks / select_test).  Config sets the
        current-sense scaling and limits the calibration relies on."""
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            wx.MessageBox(
                "Configuration is required after a firmware update before you can calibrate.\n\n"
                "Apply the puck configuration first, then run calibration.",
                "Configuration Required", wx.OK | wx.ICON_WARNING)
            return False
        return True

    def _calibrate_board(self, quick):
        """Calibrate All, Thorough or Quick: p4sysid.boardcal.calibrate_board,
        the sequence puckutility's CLI, pucktuner and p4gui run too, in
        dependency-stage order -- encoder test, bias, gain, slope, the
        slope's baseline fold, then the encoder zero.  The slope and the
        drive-gated offset are cleared first so bias/gain measure the raw
        current.  *quick*: the quick bias, a 4-level slope sweep and the
        coarser zero (8 steps per electrical cycle, not 16); every stored
        value matches the Thorough run within tolerance."""
        if not self._config_gate():
            return False
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sequence
        print("Running {} calibration for Puck {}".format(
            'QUICK' if quick else 'full', self.getID()))
        _cal_t0 = time.time()

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()
        span = [0, 0]

        def on_step(name, i, n):
            span[:] = self._BOARD_GAUGE.get(name, (span[0] + span[1], 0))
            self.UpdateUI(span[0])

        reporter = _StepReporter(self, lambda v: self.UpdateUI(
            span[0] + int(v) * span[1] // 100))
        results = {}
        self.Disable()
        self._adc_quiet()
        try:
            ok = boardcal.calibrate_board(self.node, reporter, quick=quick,
                                          on_step=on_step, results=results,
                                          raise_faults=True)
            res = results.get('slope')
            self._slope_stored = bool(res and res.get('stored'))
            if res and not res.get('skipped') and 'a0' in res:
                self._slope_a0, self._slope_b0 = res['a0'], res['b0']
                self._plot_current_slope(res)
            try:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.choice_test.SetSelection(0)
                self.lastMode = 0
            except Exception:
                pass
            if ok:
                self.UpdateUI(100)
                self.requireCal = False
            else:
                print('Ending calibration...')
            self.OnTaskComplete()
            return ok
        except Exception as e:
            self._cal_fault(e)
            return False
        finally:
            self._adc_restore()
            print("{} calibration finished in {:.1f} s.".format(
                'Quick' if quick else 'Full', time.time() - _cal_t0))
            self.Enable()

    def calibrate_all(self, event):  # wxGlade: wxp3_frame.<event_handler>
        return self._calibrate_board(quick=False)

    def calibrate_quick_all_pucks(self, event):
        # QUICK cal across every scanned puck — mirrors calibrate_all_pucks but drives the fast
        # calibrate_quick per node instead of the Thorough calibrate_all.
        if getattr(self, 'requireConfig', False):
            msg = ("Configuration is required after a firmware update.\n"
                   "Apply configuration before calibrating.\n\n"
                   "Would you like to configure the active Puck now?")
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.file_to_p4(None)
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sweep
        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID)   # Move to next ID for calibration
            self.select_id(None)
            self.calibrate_quick(None)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID)       # Return to starting ID after completion
        self.select_id(None)

    def calibrate_quick(self, event):
        # QUICK calibration (~25 s/puck vs the Thorough calibrate_all's ~35 s, measured on a P4-16).
        # Same steps, registers and order as calibrate_all (boardcal); the trims are the
        # quick bias settle (0.5 -> 0.35 s), the 4-level slope sweep (7 -> 4 levels, ~8 s) and
        # the coarser zero (8 steps per electrical cycle, within ~1 deg on a P4-42).
        # Bias/gain/slope cannot share one energised session or rotation: gain needs the
        # gainfactor at unity and the slope needs the calibrated one in effect.
        return self._calibrate_board(quick=True)

    # -- the single steps: p4core.calibrate does the work ------------------
    def _begin_step(self, calAll, _upd, status):
        """The frame side of a step: take the drive over, quiet the ADC
        monitor, start the gauge.  Returns (reporter, finish)."""
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        if calAll == False:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        self.frame_statusbar.SetStatusText(status, 1)
        self.frame_statusbar.Update()
        _yield()

        def finish():
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()

        return _StepReporter(self, _upd), finish

    def _menu_entry(self, calAll):
        """A step run from its own menu item: check, take over, disable."""
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self._menu_idle_takeover()
            self.Disable()
        return True

    def _run_step(self, calAll, _upd, status, fn, on_error=None):
        """Run one p4core step.  True on success; False when the user
        declined to continue; in a sequence (calAll) other errors propagate,
        from a menu item they go to _cal_fault.  *on_error(exc)* may turn a
        CalibrationError into an answer (the encoder zero asks to go on)."""
        if not self._menu_entry(calAll):
            return False
        reporter, finish = self._begin_step(calAll, _upd, status)
        try:
            try:
                fn(reporter)
            except calibrate.CalibrationDeclined as exc:
                print(exc)
                finish()
                return False
            except calibrate.CalibrationError as exc:
                if on_error is None:
                    raise
                answer = on_error(exc)
                finish()
                return answer
            finish()
            return True
        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def calibrate_ibias(self, event, calAll=False, _upd=None, quick=False):  # wxGlade: wxp3_frame.<event_handler>
        options = calibrate.BIAS_MENU_QUICK if quick else calibrate.BIAS_MENU
        return self._run_step(
            calAll, _upd, "Calibrating ibias...",
            lambda r: calibrate.calibrate_bias(self.node, r, **options))

    def calibrate_igainfactor(self, event, calAll=False, _upd=None):  # wxGlade: wxp3_frame.<event_handler>
        return self._run_step(
            calAll, _upd, "Calibrating igainfactor...",
            lambda r: calibrate.calibrate_gain(self.node, r, **calibrate.GAIN_MENU))

    def calibrate_enczero(self, event, calAll=False, _upd=None, quick=False):  # wxGlade: wxp3_frame.<event_handler>
        # Revolution-sweep electrical zero (p4core.calibrate.calibrate_ezero): the rotor turns ONE
        # FULL REVOLUTION each way.  quick=True: 8 steps per electrical cycle instead of 16.
        steps = calibrate.EZERO_QUICK_STEPS if quick else 16

        def failed(exc):
            print('Encoder Zero Failed! {}'.format(exc))
            return self._prompt('Warning!', "Encoder Zero Failed!\n\n{}\n\n"
                                "Would you like to continue calibration?".format(exc))

        return self._run_step(
            calAll, _upd, "Calibrating encoder zero (one revolution each way)...",
            lambda r: calibrate.calibrate_ezero(self.node, r, steps=steps),
            on_error=failed)

    def calibrate_encdir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_encdir' not implemented!")
        event.Skip()
