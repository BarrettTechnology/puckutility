"""cal_board.py — Current-sense bias and gain, the actuator calibration's
steps 2 and 3, from their own menu items.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it), with
the frame helpers the actuator calibration (cal_actuator.py) shares: the
config gate and the ADC monitor's quiet/restore.  p4core.calibrate does the
work, as it does in p4sysid's bias and gain steps.
"""
from p4core.cia402 import MODE_IDLE
from p4core import calibrate
from .cal_common import _yield, GuiCalReporter


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

    # -- the single steps: p4core.calibrate does the work ------------------
    def _run_step(self, status, fn):
        """Run one p4core step from its menu item: take the drive over, quiet
        the ADC monitor, drive the gauge.  True on success; False when the
        user declined to continue or it failed (the fault goes to
        _cal_fault)."""
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()
        self.Disable()
        if self.choice_test.GetSelection() != 0:
            self.lastMode = 0
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        self._adc_quiet()
        self.OnStartTask(None)
        self.frame_statusbar.SetStatusText(status, 1)
        self.frame_statusbar.Update()
        _yield()
        try:
            fn(_StepReporter(self, self.UpdateUI))
            return True
        except calibrate.CalibrationDeclined as exc:
            print(exc)
            return False
        except Exception as exc:
            self._cal_fault(exc)
            return False
        finally:
            self._adc_restore()
            self.OnTaskComplete()
            self.Enable()

    def calibrate_ibias(self, event):  # wxGlade: wxp3_frame.<event_handler>
        return self._run_step(
            "Calibrating ibias...",
            lambda r: calibrate.calibrate_bias(self.node, r,
                                               **calibrate.BIAS_MENU))

    def calibrate_igainfactor(self, event):  # wxGlade: wxp3_frame.<event_handler>
        return self._run_step(
            "Calibrating igainfactor...",
            lambda r: calibrate.calibrate_gain(self.node, r,
                                               **calibrate.GAIN_MENU))
