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

    def calibrate_all(self, event):  # wxGlade: wxp3_frame.<event_handler>
        # Try to add calibrate all step!
        # Backstop for the config gate: never drive control modes for calibration
        # while a firmware flash is still pending configuration (see
        # calibrate_all_pucks / select_test). Config sets the current-sense scaling
        # and limits the calibration relies on.
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            wx.MessageBox(
                "Configuration is required after a firmware update before you can calibrate.\n\n"
                "Apply the puck configuration first, then run calibration.",
                "Configuration Required", wx.OK | wx.ICON_WARNING)
            return False
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sequence
        print("Running full calibration for Puck {}".format(self.getID()))
        _cal_t0 = time.time()   # baseline timer: total start-to-finish for the full cal

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        try:
            # Disable the Current Sense Slope correction for the whole sequence. It distorts the raw
            # alpha/beta current, so Bias/Gain/enczero MUST run without it -- a stale or garbage slope
            # otherwise poisons the Gain cal (alpha reads ~half -> "Beta Gainfactor out of bounds")
            # and the whole cal cascades. The Slope step at the end re-measures and re-stores it.
            calibrate.clear_slope(self.node)
            self._clear_offset_reg()   # v3+: drive-gated offset applied under drive -> clear it too so
                                       # Bias/Gain/Slope all measure RAW current (else overshoot/brownout).

            continueCal = self.test_encoder(None, True)
            self.Disable()
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(5)
            continueCal = self.calibrate_ibias(None, True,
                              _upd=lambda v: self.UpdateUI(5 + v * 15 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(20)
            continueCal = self.calibrate_igainfactor(None, True,
                              _upd=lambda v: self.UpdateUI(20 + v * 52 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(72)
            # Re-measure the Current Sense Slope we cleared at the top (Bias+Gain are fresh now).
            # ROBUSTNESS: the slope is a REFINEMENT that streams over the FastPDO path, which can hit a
            # transient SYNC/SDO comms glitch (0x05040000 / "No SDO response"). That must NOT abort the
            # whole cal -- the slope was cleared at the top, so a failure just leaves it OFF (safe). Retry
            # once (transients clear), then continue to enczero + the fold regardless.
            for _slope_try in (1, 2):
                try:
                    self.calibrate_current_slope(None, True, force_sdo=(_slope_try == 2))
                    break
                except Exception as _slope_err:
                    print("  Current Sense Slope attempt {}/2 failed: {}".format(_slope_try, _slope_err))
                    # leave the drive SAFE before retry/continue
                    calibrate.deenergise(self.node, settle_s=0, clear_fault=True)
                    if _slope_try == 2:
                        print("  Slope left OFF (not stored); continuing the calibration.")
                        self._slope_stored = False

            self.UpdateUI(85)
            self.calibrate_enczero(None, True,
                              _upd=lambda v: self.UpdateUI(85 + v * 15 // 100))

            # FINAL STEP: fold the slope cal's fitted drive-on baseline (a0/b0) into the iSense Bias so
            # the current loop sees the true current under drive — the fixed offset the slope can't
            # remove (validated: makes low-settling smooth). Runs LAST, after every Bias write, so
            # nothing wipes it. Only when the slope stored a trustworthy geared fit (skips direct-drive,
            # where a0/b0 are degenerate). enczero does not touch Bias, so the fold's base is still fresh.
            if getattr(self, '_slope_stored', False):
                self.fold_baseline_offset(None, calAll=True)
            else:
                print("Baseline fold skipped (slope not stored — direct-drive / untrustworthy fit).")

            self.OnTaskComplete()
            self.requireCal = False
        except Exception as e:
            self._cal_fault(e)
        finally:
            print("Full calibration finished in {:.1f} s.".format(time.time() - _cal_t0))
            self.Enable()
        #event.Skip()

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
        #
        # Same OD writes / same registers / same values as calibrate_all — the sole intended
        # accuracy trade is the coarser enczero (calibrate_enczero(quick=True)): 8 steps per
        # electrical cycle instead of 16 over the same revolution sweep, within ~1 deg on a P4-42.
        # Everything else runs the identical Thorough step so Quick and Thorough
        # produce equivalent stored cal within tolerance.
        #
        # FOLDS EVALUATED BUT NOT APPLIED (correctness over speed, per design):
        #   * Shared drive session across ibias/gain/slope: each step function independently arms
        #     (CLEAR_FAULT/SHUTDOWN/OP_ENABLED) and drops to MODE_IDLE, and interleaves mode-specific
        #     ramps/settles. Threading one energised session through them safely needs an invasive
        #     refactor of the Thorough-path functions (untestable here) — the fold was NOT safe, so
        #     the steps are called as-is (identical OD writes, drive re-armed per step).
        #   * Gain folded into the slope's top level: the gain fit REQUIRES gainfactor=4096 (unity)
        #     and reads Alpha/Beta fundamentals, while the slope step REQUIRES the calibrated
        #     gainfactor already in effect and reads id/iq offsets at detented angles — conflicting
        #     preconditions. Sharing one rotation would corrupt BOTH cals (worst case: resets gain to
        #     unity). NOT safe to fold without firmware-internal confirmation + hardware validation,
        #     so gain and slope run as separate Thorough steps.
        # The realized Quick speedup comes from three SAFE, quick-gated trims (Thorough untouched):
        #   * calibrate_current_slope(quick=True): the 18.5 s sweep drops 7->4 (weighted-low) current
        #     levels — the dominant saving (~8 s). Same writes/model/gates; per-level offset quality
        #     (N angles, M samples, fwd+rev hysteresis cancel) is identical, only the level COUNT falls.
        #   * calibrate_ibias(quick=True): the pre-average iSense settle 0.5 s -> 0.35 s (~0.15 s).
        #   * calibrate_enczero(quick=True): coarser spiral-gated spin (auto-falls-back to the fine sweep).
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            wx.MessageBox(
                "Configuration is required after a firmware update before you can calibrate.\n\n"
                "Apply the puck configuration first, then run calibration.",
                "Configuration Required", wx.OK | wx.ICON_WARNING)
            return False
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sequence
        print("Running QUICK calibration for Puck {}".format(self.getID()))
        _cal_t0 = time.time()

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        try:
            # Clear the Current Sense Slope for the whole sequence (same as calibrate_all): a stale
            # slope distorts raw alpha/beta and poisons Bias/Gain. The Slope step re-measures it.
            calibrate.clear_slope(self.node)
            self._clear_offset_reg()   # v3+: clear drive-gated offset so Bias/Gain/Slope measure RAW current

            continueCal = self.test_encoder(None, True)
            self.Disable()
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(5)
            continueCal = self.calibrate_ibias(None, True, quick=True,
                              _upd=lambda v: self.UpdateUI(5 + v * 15 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(20)
            continueCal = self.calibrate_igainfactor(None, True,
                              _upd=lambda v: self.UpdateUI(20 + v * 52 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(72)
            # Current Sense Slope — same robustness (retry once, continue if it can't store).
            # quick=True trims the sweep to 4 (weighted-low) current levels; same writes/model/gates.
            for _slope_try in (1, 2):
                try:
                    self.calibrate_current_slope(None, True, force_sdo=(_slope_try == 2), quick=True)
                    break
                except Exception as _slope_err:
                    print("  Current Sense Slope attempt {}/2 failed: {}".format(_slope_try, _slope_err))
                    # leave the drive SAFE before retry/continue
                    calibrate.deenergise(self.node, settle_s=0, clear_fault=True)
                    if _slope_try == 2:
                        print("  Slope left OFF (not stored); continuing the calibration.")
                        self._slope_stored = False

            self.UpdateUI(85)
            # QUICK enczero: spiral-gated, auto-falls-back to the fine kinetic sweep on a bad spread.
            self.calibrate_enczero(None, True, quick=True,
                              _upd=lambda v: self.UpdateUI(85 + v * 15 // 100))

            # Same final baseline fold as Thorough (only on a trustworthy geared slope fit).
            if getattr(self, '_slope_stored', False):
                self.fold_baseline_offset(None, calAll=True)
            else:
                print("Baseline fold skipped (slope not stored — direct-drive / untrustworthy fit).")

            self.OnTaskComplete()
            self.requireCal = False
        except Exception as e:
            self._cal_fault(e)
        finally:
            print("Quick calibration finished in {:.1f} s.".format(time.time() - _cal_t0))
            self.Enable()

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
