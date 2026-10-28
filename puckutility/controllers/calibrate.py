"""calibrate.py — puckutility's actuator calibration without a window.

A manufacturing batch shares one configuration CSV, loaded first (flash,
config load, Save.All, reboot).  The actuator calibration is then what the
CSV cannot carry, measured on each unit: p4core.actuator.plans.ACTUATOR
(encoder, current-sense bias, gain, slope and fold, electrical zero, runout
table, cogging).  The steps are p4core's, run through a p4core.actuator
Session in STEPS order; pucktuner and p4gui run the same ones.  The loop
gains are not searched here: pucktuner does that.  This keeps puckutility's
entry points to them (the CLI's calibrate, the system-config calibrate
hook); the GUI's Actuator Calibration menu (gui/cal_actuator.py) calls the
same functions with dialogs and the progress bar around them.  No wx here.

A step the drive's firmware cannot run (plans.unavailable) is not offered:
supported() is what the menus list and what a whole calibration runs.

Nothing is saved to NV (Save.All) unless the caller asks: *save* here, the
CLI's --save, a question in the GUI.  A few steps save their own register
to take effect (p4core.actuator.steps), as they always have.
"""

from p4core.reporter import Reporter
from p4core.actuator import plans


class CalibrationFailed(RuntimeError):
    pass


#: The actuator calibration's steps, in the order they run and the menus
#: number them: key -> title.
STEPS = {k: plans.title(k) for k in plans.run_order(plans.ACTUATOR)}


def _reporter(reporter):
    return reporter if reporter is not None else Reporter()


def supported(node):
    """The actuator steps *node*'s firmware can run, in run order."""
    return tuple(plans.run_order(plans.available_keys(node, plans.ACTUATOR)))


def numbered(keys):
    """[(n, key, title), ...]: *keys* in run order, numbered from 1."""
    return [(n, k, plans.title(k))
            for n, k in enumerate(plans.run_order(keys), 1)]


def step_keys(names):
    """Step keys for *names* (keys or plans.ALIASES names), actuator steps
    only, in run order; CalibrationFailed names the choices otherwise."""
    try:
        keys = [plans.canonical(n, plans.ACTUATOR) for n in names]
    except plans.PlanError as exc:
        raise CalibrationFailed(str(exc)) from None
    return plans.run_order(keys)


def make_session(node, reporter=None, reboot=None, sync=None):
    """A p4core.actuator Session on *node* reporting to *reporter*.  *reboot()*
    NMT-resets the drive and returns its node (an encoder filter change
    takes effect only at boot); without it that step stops and asks for a
    reboot.  *sync*: the SYNC's owner (controllers.sync.SyncOwner), so a
    measurement stream puts back the rate it found."""
    return plans.make_session(node, _reporter(reporter), sync=sync,
                              reboot=reboot)


def calibrate_actuator(node, reporter=None, steps=None, reboot=None,
                       save=False, network=None, session=None):
    """The actuator calibration on *node*: every step its firmware can run
    (supported), or just the named *steps*, in run order, stopping at the
    first failure.
    *session*: a Session to run in (make_session), so a step can use an
    earlier one's result (fold needs this session's slope sweep).  *save*:
    Save.All when every step passed; otherwise the results stay in RAM.
    Returns (ok, session)."""
    reporter = _reporter(reporter)
    keys = step_keys(steps) if steps else list(supported(node))
    if session is None:
        session = make_session(node, reporter, reboot)
    ok = plans.run(session, keys, reporter)
    if ok and save:
        ok = save_results(session, reporter)
    return ok, session


def save_results(session, reporter=None):
    """Save.All, and the reboot an i_cont change needs (plans.save).  The
    caller asks the user first."""
    return plans.save(session, reporter=reporter)


def summary_text(session, steps=None):
    """One line per step: number, title, verdict, detail.  Without *steps*,
    the steps the drive's firmware can run (supported)."""
    return plans.summary_text(session, step_keys(steps) if steps
                              else supported(session.dev.node))
