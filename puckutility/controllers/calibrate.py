"""calibrate.py — puckutility's calibrations without a window.

The calibration routines live in gui/calibrate_menu.py, a mixin written for
the main frame.  HeadlessCalibrator gives it the few frame attributes it
touches (a status bar, the test-mode choice, prompts) and routes them to a
Reporter, so the CLI and the GUI run the same code.  The calibrations with
``calAll=True`` never open a dialog.

calibrate_menu imports wx (the module defines dialogs), so these need wxPython
installed, but not a display: without a wx.App its event pumping is skipped.
"""

import time

from p4core import can_backend
from p4core.reporter import Reporter


class CalibrationFailed(RuntimeError):
    pass


def _mixin():
    try:
        from ..gui.calibrate_menu import calibrate
    except ImportError as exc:
        raise CalibrationFailed(
            'calibration needs wxPython (pip install "puckutility[gui]"): {}'
            .format(exc)) from exc
    return calibrate


class _StatusBar:
    def __init__(self, reporter):
        self.reporter = reporter

    def SetStatusText(self, text, number=0):
        if number == 0 and text:
            self.reporter.status(text)

    def Update(self):
        pass

    def Refresh(self):
        pass


class _Choice:
    def GetSelection(self):
        return 0

    def SetSelection(self, value):
        pass


def make_calibrator(node, network=None, reporter=None):
    """The calibrate mixin bound to *node*, reporting to *reporter*."""
    reporter = reporter if reporter is not None else Reporter()

    class HeadlessCalibrator(_mixin()):
        _PRODUCT_CODE_MODELS = can_backend.PRODUCT_CODE_MODELS

        def __init__(self):
            self.node = node
            self.network = network if network is not None else node.network
            self.ID = node.id
            self.ADC_ON = False
            self.adcWasON = False
            self.lastMode = 0
            self.requireCal = True
            self.reporter = reporter
            self.frame_statusbar = _StatusBar(reporter)
            self.choice_test = _Choice()

        # The frame interface the mixin expects.
        def getID(self):
            return self.node.id

        def check_for_node(self):
            return True

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

        def _prompt(self, title, msg):
            return reporter.confirm('{}: {}\nContinue calibration?'.format(
                title, msg), default=False)

        def _prompt_ok(self, title, msg):
            reporter.warn('{}: {}'.format(title, msg))

    return HeadlessCalibrator()


def _clear_slope(node):
    """Bias/Gain must measure the raw offset, not one the firmware is
    already correcting: clear the Current Sense Slope first (as the GUI's
    Calibrate All does)."""
    try:
        node.sdo[0x3008][7].raw = 0
        node.sdo[0x3009][7].raw = 0
        node.sdo['Save']['Single'].raw = (0x3008 << 8) | 0x07
        node.sdo['Save']['Single'].raw = (0x3009 << 8) | 0x07
    except Exception:
        pass


def _none(value):
    pass


def calibrate_all(node, network=None, reporter=None, quick=False):
    """Encoder test, current bias, gain, sense slope, encoder zero, and the
    baseline fold.  *quick* uses the coarser encoder zero (8 steps per
    electrical cycle, not 16) and a 4-level slope fit; every stored value
    matches the thorough run within tolerance.  Returns True on success."""
    reporter = reporter if reporter is not None else Reporter()
    cal = make_calibrator(node, network, reporter)
    t0 = time.time()
    reporter.status('Running the {}calibration sequence for node {}...'.format(
        'QUICK ' if quick else '', node.id))
    _clear_slope(node)
    if not cal.test_encoder(None, calAll=True):
        reporter.warn('Calibration aborted at the encoder test')
        return False
    ibias = dict(quick=True) if quick else {}
    if not cal.calibrate_ibias(None, calAll=True, _upd=_none, **ibias):
        reporter.warn('Calibration aborted at the current bias')
        return False
    if not cal.calibrate_igainfactor(None, calAll=True, _upd=_none):
        reporter.warn('Calibration aborted at the current gain')
        return False
    # Current Sense Slope: retry once on a transient SYNC/SDO glitch, then go
    # on (a failure leaves the slope OFF, which is safe: it was cleared
    # above).  Skipped by the mixin on firmware < 4.4.0.
    slope = dict(quick=True) if quick else {}
    for attempt in (1, 2):
        try:
            cal.calibrate_current_slope(None, calAll=True,
                                        force_sdo=(attempt == 2), **slope)
            break
        except Exception as exc:
            reporter.warn('Current Sense Slope attempt {}/2 failed: {}'
                          .format(attempt, exc))
            cal._slope_stored = False
    # calibrate_enczero returns False only on a user-requested abort.
    enczero = dict(quick=True) if quick else {}
    if cal.calibrate_enczero(None, calAll=True, _upd=_none, **enczero) is False:
        reporter.warn('Calibration aborted at the encoder zero')
        return False
    # The baseline fold only when the slope stored a trustworthy fit.
    if getattr(cal, '_slope_stored', False):
        cal.fold_baseline_offset(None, calAll=True)
    reporter.note('Calibration complete in {:.1f} s'.format(time.time() - t0))
    return True


#: Single calibrations: name -> (help, call on the calibrator).
STEPS = {
    'encoder': ('test the encoder',
                lambda cal: cal.test_encoder(None, calAll=True)),
    'bias': ('current-sense bias',
             lambda cal: cal.calibrate_ibias(None, calAll=True, _upd=_none)),
    'gain': ('current-sense gain factor',
             lambda cal: cal.calibrate_igainfactor(None, calAll=True,
                                                   _upd=_none)),
    'enczero': ('encoder electrical zero (the motor turns)',
                lambda cal: cal.calibrate_enczero(None, calAll=True,
                                                  _upd=_none)),
    'settling': ('ADC settling time (MaxSettlingTime), applied and saved',
                 lambda cal: cal.calibrate_itiming(None, calAll=True)),
    'slope': ('current-sense slope',
              lambda cal: cal.calibrate_current_slope(None, calAll=True)),
}


def run_step(name, node, network=None, reporter=None):
    """One calibration by name; False means it failed or was declined."""
    if name not in STEPS:
        raise CalibrationFailed('unknown calibration {!r}; choose from {}'
                                .format(name, ', '.join(STEPS)))
    cal = make_calibrator(node, network, reporter)
    return STEPS[name][1](cal) is not False
