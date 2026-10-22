"""calibrate.py — puckutility's actuator calibration without a window.

A manufacturing batch shares one configuration CSV, loaded first (flash,
config load, Save.All, reboot).  The actuator calibration is then what the
CSV cannot carry, measured on each unit: p4sysid.plans.ACTUATOR (encoder,
current-sense bias, gain, slope and fold, electrical zero, runout table,
cogging, velocity and position refine).  The steps are p4sysid's, run
through a p4sysid Session in STEPS order; pucktuner and p4gui run the same
ones.  This keeps puckutility's entry points to them (the CLI's calibrate,
the system-config calibrate hook); the GUI's Actuator Calibration menu
(gui/cal_actuator.py) calls the same functions with dialogs and the
progress bar around them.  No wx here.

Nothing is saved to NV (Save.All) unless the caller asks: *save* here, the
CLI's --save, a question in the GUI.  A few steps save their own register
to take effect (p4sysid.steps), as they always have.
"""

from p4core.reporter import Reporter
from p4sysid import plans


class CalibrationFailed(RuntimeError):
    pass


#: The actuator calibration's steps, in the order the menus number them
#: (plans.numbered): key -> title.
STEPS = {st.key: st.title for _n, _stage, st in plans.numbered(plans.ACTUATOR)}


def _reporter(reporter):
    return reporter if reporter is not None else Reporter()


def step_keys(names):
    """Step keys for *names* (keys or plans.ALIASES names), actuator steps
    only, in run order; CalibrationFailed names the choices otherwise."""
    try:
        keys = [plans.canonical(n, plans.ACTUATOR) for n in names]
    except plans.PlanError as exc:
        raise CalibrationFailed(str(exc)) from None
    return plans.run_order(keys)


def make_session(node, reporter=None, reboot=None):
    """A p4sysid Session on *node* reporting to *reporter*.  *reboot()*
    NMT-resets the drive and returns its node (an encoder filter change
    takes effect only at boot); without it that step stops and asks for a
    reboot."""
    return plans.make_session(node, _reporter(reporter), reboot=reboot)


def calibrate_actuator(node, reporter=None, steps=None, reboot=None,
                       save=False, network=None, session=None):
    """The actuator calibration on *node*: every step of plans.ACTUATOR, or
    just the named *steps*, in run order, stopping at the first failure.
    *session*: a Session to run in (make_session), so a step can use an
    earlier one's result (fold needs this session's slope sweep).  *save*:
    Save.All when every step passed; otherwise the results stay in RAM.
    Returns (ok, session)."""
    reporter = _reporter(reporter)
    keys = step_keys(steps) if steps else list(plans.ACTUATOR)
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
    """One line per step: number, title, verdict, detail."""
    return plans.summary_text(session, step_keys(steps) if steps
                              else plans.ACTUATOR)
