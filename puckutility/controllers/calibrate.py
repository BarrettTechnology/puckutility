"""calibrate.py — puckutility's calibrations without a window.

The calibrations are p4core.calibrate (bias, gain, encoder zero) and p4sysid
(encoder test, settling, slope, baseline fold); this runs them in the GUI's
Calibrate All order on a node and a Reporter.  The GUI's Calibrate menu
(gui/cal_board.py and friends) calls the same functions, with dialogs and
the progress bar around them.  No wx here.
"""

import time

from p4core import calibrate as cal
from p4core.reporter import Reporter
from p4sysid import drivecal, enctest, settling, slope


class CalibrationFailed(RuntimeError):
    pass


def _reporter(reporter):
    return reporter if reporter is not None else Reporter()


def _enczero(node, reporter, quick=False):
    """Encoder zero; on a failed sweep (nothing stored) ask whether to go
    on.  False means stop."""
    steps = cal.EZERO_QUICK_STEPS if quick else 16
    try:
        cal.calibrate_ezero(node, reporter, steps=steps)
    except cal.CalibrationDeclined:
        raise
    except cal.CalibrationError as exc:
        return reporter.confirm('Encoder zero failed\n{}\n\nContinue calibration?'
                                .format(exc), default=False)
    return True


def calibrate_all(node, network=None, reporter=None, quick=False):
    """Encoder test, current bias, gain, sense slope, encoder zero, and the
    baseline fold.  *quick* uses the coarser encoder zero (8 steps per
    electrical cycle, not 16), the quick bias and a 4-level slope fit; every
    stored value matches the thorough run within tolerance.  Returns True on
    success."""
    reporter = _reporter(reporter)
    t0 = time.time()
    reporter.status('Running the {}calibration sequence for node {}...'.format(
        'QUICK ' if quick else '', node.id))
    try:
        # Bias/Gain must measure the raw current, not one the firmware is
        # already correcting: clear the slope and the drive-gated offset.
        cal.clear_slope(node)
        drivecal.clear_offset_reg(node)
        bias = cal.BIAS_MENU_QUICK if quick else cal.BIAS_MENU
        for label, fn in (
                ('encoder test', lambda: enctest.test_encoder(node, reporter)),
                ('current bias', lambda: cal.calibrate_bias(node, reporter,
                                                            **bias)),
                ('current gain', lambda: cal.calibrate_gain(
                    node, reporter, **cal.GAIN_MENU))):
            try:
                fn()
            except cal.CalibrationError as exc:
                reporter.warn('Calibration aborted at the {}: {}'.format(label, exc))
                return False
        # Current Sense Slope: retry once on the SDO path after a transient
        # SYNC/SDO glitch, then go on (a failure leaves the slope OFF, which is
        # safe: it was cleared above).  Skipped on firmware < 4.4.0.
        res = None
        for attempt in (1, 2):
            try:
                res = slope.calibrate_current_slope(
                    node, reporter, force_sdo=(attempt == 2), quick=quick)
                break
            except Exception as exc:
                reporter.warn('Current Sense Slope attempt {}/2 failed: {}'
                              .format(attempt, exc))
                cal.deenergise(node, settle_s=0, clear_fault=True)
        try:
            go_on = _enczero(node, reporter, quick)
        except cal.CalibrationDeclined:
            go_on = False
        if not go_on:
            reporter.warn('Calibration aborted at the encoder zero')
            return False
        # The baseline fold only when the slope stored a trustworthy fit.
        if res and res.get('stored'):
            slope.fold_baseline_offset(node, res, reporter)
        else:
            reporter.note('Baseline fold skipped (slope not stored)')
    except Exception as exc:
        reporter.warn(drivecal.fault_message(node, exc))
        return False
    reporter.note('Calibration complete in {:.1f} s'.format(time.time() - t0))
    return True


def _encoder(node, reporter):
    enctest.test_encoder(node, reporter)


def _bias(node, reporter):
    cal.calibrate_bias(node, reporter, **cal.BIAS_MENU)


def _gain(node, reporter):
    cal.calibrate_gain(node, reporter, **cal.GAIN_MENU)


def _settling(node, reporter):
    r = settling.calibrate_settling(node, reporter, clear_stale_slope=True)
    if r.get('slope_cleared'):
        reporter.warn('MaxSettlingTime changed, so the current-sense slope '
                      'was cleared: run the slope calibration again')


def _slope_step(node, reporter):
    res = slope.calibrate_current_slope(node, reporter)
    return res.get('skipped') != 'firmware'


#: Single calibrations: name -> (help, fn(node, reporter) -> bool or None).
STEPS = {
    'encoder': ('test the encoder', _encoder),
    'bias': ('current-sense bias', _bias),
    'gain': ('current-sense gain factor', _gain),
    'enczero': ('encoder electrical zero (the motor turns)', _enczero),
    'settling': ('ADC settling time (MaxSettlingTime), applied and saved',
                 _settling),
    'slope': ('current-sense slope', _slope_step),
}


def run_step(name, node, network=None, reporter=None):
    """One calibration by name; False means it failed or was declined."""
    if name not in STEPS:
        raise CalibrationFailed('unknown calibration {!r}; choose from {}'
                                .format(name, ', '.join(STEPS)))
    reporter = _reporter(reporter)
    try:
        return STEPS[name][1](node, reporter) is not False
    except cal.CalibrationError as exc:
        reporter.warn(str(exc))
        return False
    except Exception as exc:
        reporter.warn(drivecal.fault_message(node, exc))
        return False
