"""cli.py — puckutility's command line.

    puckutility scan
    puckutility info [--all] [--no-flashloader]
    puckutility flash FIRMWARE [--all]
    puckutility config load CSV [--all]       (and export/check/templates)
    puckutility calibrate [STEP ...] [--save] [--all]
    puckutility system-config INI
    puckutility set-id NEW_ID
    puckutility reset [--all]
    puckutility flash-canable [FIRMWARE]

scan, flash, config, set-id and reset are p4core's; calibrate runs the
actuator calibration the GUI runs (controllers.calibrate, p4sysid's steps).  main.py turns the old
flat flags (--flash FW, --calibrate, ...) into these.
"""

import argparse
import sys

from p4core import can_backend, cli, ops

from . import __version__
from .controllers import calibrate as _calibrate
from .controllers.system_config import SystemConfigError
from .model import PuckModel


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def _read(node, name, sub=None):
    try:
        var = node.sdo[name] if sub is None else node.sdo[name][sub]
        return var.raw
    except Exception:
        return None


def _cmd_info(ctx):
    results = []
    for node_id in cli.targets(ctx):
        # The puckutility extras first, while the application is running:
        # the flashloader read reboots the puck.
        node = ctx.session.add_node(node_id)
        extra = dict(settling_ns=_read(node, 'Amp', 'MaxSettlingTime'),
                     encoder_comp=_read(node, 0x3027, 1))
        result = ops.info(ctx.session, node_id,
                          flashloader=not ctx.args.no_flashloader,
                          reporter=ctx.reporter)
        result.update(extra)
        results.append(result)
    lines = []
    for r in results:
        lines += ops.format_info(r)
        lines.append('  ADC settling: {}'.format(
            'unknown' if r['settling_ns'] is None
            else '{} ns'.format(r['settling_ns'])))
        lines.append('  Enc comp:     {}'.format(
            'unknown' if r['encoder_comp'] is None
            else 'ON' if r['encoder_comp'] else 'OFF'))
    ctx.emit(results if ctx.args.all else results[0], lines)


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------
def _reboot(ctx, node_id):
    """The reboot an encoder filter change needs: NMT reset, wait for the
    application, the node again (p4core Session.reset_node)."""
    return lambda: ctx.session.reset_node(node_id)


def _calibrate_node(ctx, node_id, steps=None, save=False):
    """The actuator calibration on *node_id*, its summary printed.  Returns
    (ok, session): ok when every step passed (and, with *save*, Save.All
    did)."""
    node = ctx.session.add_node(node_id)
    ok, session = _calibrate.calibrate_actuator(
        node, ctx.reporter, steps=steps, reboot=_reboot(ctx, node_id),
        save=save, network=ctx.session.network)
    ctx.reporter.note('node {}:\n{}'.format(
        node_id, _calibrate.summary_text(session, steps)))
    if save and ok:
        ctx.reporter.note('node {}: saved to NV (Save.All)'.format(node_id))
    elif session.unsaved:
        ctx.reporter.note('node {}: results are in RAM only (run with --save '
                          'to keep them)'.format(node_id))
    return ok, session


def _step_name(name):
    """A STEP argument: an actuator step, checked before the bus opens."""
    try:
        _calibrate.step_keys([name])
    except _calibrate.CalibrationFailed as exc:
        raise argparse.ArgumentTypeError(
            '{} (the actuator calibration steps; the batch CSV carries the '
            'rest)'.format(exc))
    return name


def _cmd_calibrate(ctx):
    steps = ctx.args.steps
    if not ctx.reporter.confirm(
            'Calibration turns the motor, continuously for cogging and the '
            'refine steps. Is the output free to turn?', default=True):
        return cli.EXIT_FAILED
    ok = True
    for node_id in cli.targets(ctx):
        ctx.reporter.status('--- Node {} ---'.format(node_id))
        if not _calibrate_node(ctx, node_id, steps or None,
                               ctx.args.save)[0]:
            ctx.reporter.warn('node {}: calibration failed'.format(node_id))
            ok = False
    return cli.EXIT_OK if ok else cli.EXIT_FAILED


# ---------------------------------------------------------------------------
# system-config
# ---------------------------------------------------------------------------
def _cmd_system_config(ctx):
    from .controllers import system_config

    def calibrate(node_id):
        # The actuator calibration; Save.All only when the user says so.
        ok, session = _calibrate_node(ctx, node_id)
        if ok and session.unsaved and ctx.reporter.confirm(
                'Save node {}\'s calibration to NV (Save.All)?'.format(node_id),
                default=False):
            ok = _calibrate.save_results(session, ctx.reporter)
        return ok

    outcome = system_config.apply(ctx.session, ctx.args.ini, ctx.reporter,
                                  calibrate=calibrate)
    ctx.emit({str(k): v for k, v in outcome.items()},
             ['node {}: {}'.format(k, v) for k, v in outcome.items()])
    failed = [v for v in outcome.values() if v.endswith('failed')]
    return cli.EXIT_FAILED if failed else cli.EXIT_OK


# ---------------------------------------------------------------------------
# flash-canable
# ---------------------------------------------------------------------------
def _cmd_flash_canable(ctx):
    from .controllers import canable
    ok = canable.flash_canable(ctx.args.firmware or None,
                               verbose=ctx.args.verbose)
    return cli.EXIT_OK if ok else cli.EXIT_FAILED


def build_parser():
    parser, sub = cli.make_parser(
        'puckutility',
        'Flash, configure and calibrate P4 pucks. With no command, starts '
        'the GUI.', version=__version__)
    sub.add_parser('gui', help='start the GUI (the default)')
    cli.add_common_commands(
        sub, only=('scan', 'flash', 'config', 'set-id', 'reset'))

    p = sub.add_parser('info', help='firmware, flashloader, model and live '
                       'readings (reads the flashloader by rebooting the puck)')
    p.set_defaults(func=_cmd_info, needs_bus=True)
    p.add_argument('--all', action='store_true', help='every puck on the bus')
    p.add_argument('--no-flashloader', action='store_true',
                   help="skip the flashloader version (don't reboot the puck)")

    p = sub.add_parser(
        'calibrate', help='actuator calibration (the motor turns)',
        description='The actuator calibration: the per-unit steps a batch '
        'CSV cannot carry (load the CSV first). With no STEP, all of them in '
        'order; named steps run in that order too. Results stay in RAM '
        'unless --save. Steps: ' +
        '; '.join('{}: {}'.format(k, t) for k, t in _calibrate.STEPS.items()))
    p.set_defaults(func=_cmd_calibrate, needs_bus=True)
    p.add_argument('steps', nargs='*', metavar='STEP', type=_step_name,
                   help='any of ' + ', '.join(_calibrate.STEPS))
    p.add_argument('--save', action='store_true',
                   help='Save.All when every step passed (default: RAM only)')
    p.add_argument('--all', action='store_true', help='every puck on the bus')

    p = sub.add_parser('system-config',
                       help='flash, configure (and calibrate) the pucks an '
                       'INI describes')
    p.set_defaults(func=_cmd_system_config, needs_bus=True)
    p.add_argument('ini')

    p = sub.add_parser('flash-canable',
                       help='flash CandleLight firmware into a CANable over '
                       'USB DFU (no CAN bus needed)')
    p.set_defaults(func=_cmd_flash_canable, needs_bus=False)
    p.add_argument('firmware', nargs='?',
                   help='firmware image (default: the bundled multiboard build)')
    return parser


def main(argv=None):
    return cli.main(build_parser(), argv, session_factory=PuckModel,
                    failures=(_calibrate.CalibrationFailed, SystemConfigError,
                              can_backend.CanBusUnavailable))


if __name__ == '__main__':
    sys.exit(main())
