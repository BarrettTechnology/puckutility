"""calibrate.py — puckutility's calibrations without a window.

The board calibration is p4sysid.boardcal: one sequence, in dependency-stage
order, that pucktuner and p4gui run too.  This keeps puckutility's names for
it (the CLI's calibrate STEP names and the system-config calibrate hook);
the GUI's Calibrate menu (gui/cal_board.py) calls the same boardcal with
dialogs and the progress bar around it.  No wx here.
"""

from p4core.reporter import Reporter
from p4sysid import boardcal


class CalibrationFailed(RuntimeError):
    pass


def _reporter(reporter):
    return reporter if reporter is not None else Reporter()


def calibrate_all(node, network=None, reporter=None, quick=False):
    """Encoder test, current bias, gain, sense slope, the slope's baseline
    fold, and the encoder zero (boardcal.FULL_SEQUENCE).  *quick* uses the
    coarser encoder zero (8 steps per electrical cycle, not 16), the quick
    bias and a 4-level slope fit; every stored value matches the thorough
    run within tolerance.  Returns True on success."""
    return boardcal.calibrate_board(node, _reporter(reporter), quick=quick)


#: Single calibrations: name -> (help, None).  Run in dependency-stage order
#: whatever order they are named in (run_steps).
STEPS = {name: (boardcal.HELP[name], None) for name in boardcal.BOARD_STEPS}


def run_steps(names, node, network=None, reporter=None):
    """The named calibrations, in stage order; False at the first that
    failed or was declined."""
    unknown = [n for n in names if n not in STEPS and n not in boardcal.ALIASES]
    if unknown:
        raise CalibrationFailed('unknown calibration {!r}; choose from {}'
                                .format(unknown[0], ', '.join(STEPS)))
    return boardcal.calibrate_board(node, _reporter(reporter), steps=names)


def run_step(name, node, network=None, reporter=None):
    """One calibration by name; False means it failed or was declined."""
    return run_steps([name], node, network, reporter)
