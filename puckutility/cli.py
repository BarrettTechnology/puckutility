"""cli.py — puckutility's command line.

    puckutility scan
    puckutility info [--all] [--no-flashloader]
    puckutility flash FIRMWARE [--all]
    puckutility config load CSV [--all]       (and export/check/templates)
    puckutility calibrate [STEP ...] [--quick] [--all]
    puckutility system-config INI
    puckutility set-id NEW_ID
    puckutility reset [--all]
    puckutility flash-canable [FIRMWARE]

scan, flash, config, set-id and reset are p4core's; calibrate runs the GUI's
own calibration routines (controllers.calibrate).  main.py turns the old
flat flags (--flash FW, --calibrate, ...) into these.
"""

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
def _cmd_calibrate(ctx):
    steps = ctx.args.steps
    unknown = set(steps) - set(_calibrate.STEPS)
    if unknown:
        ctx.reporter.warn('unknown calibration(s) {}; choose from {}'.format(
            ', '.join(sorted(unknown)), ', '.join(_calibrate.STEPS)))
        return cli.EXIT_USAGE
    if ctx.args.quick and steps:
        ctx.reporter.warn('--quick applies to the full sequence only')
        return cli.EXIT_USAGE
    if not ctx.reporter.confirm(
            'Calibration turns the motor. Is it free to turn?', default=True):
        return cli.EXIT_FAILED
    ok = True
    for node_id in cli.targets(ctx):
        node = ctx.session.add_node(node_id)
        ctx.reporter.status('--- Node {} ---'.format(node_id))
        if steps:
            for step in steps:
                if not _calibrate.run_step(step, node, ctx.session.network,
                                           ctx.reporter):
                    ctx.reporter.warn('node {}: {} failed'.format(node_id, step))
                    ok = False
                    break
        elif not _calibrate.calibrate_all(node, ctx.session.network,
                                          ctx.reporter, quick=ctx.args.quick):
            ok = False
    return cli.EXIT_OK if ok else cli.EXIT_FAILED


# ---------------------------------------------------------------------------
# system-config
# ---------------------------------------------------------------------------
def _cmd_system_config(ctx):
    from .controllers import system_config

    def calibrate(node_id):
        node = ctx.session.add_node(node_id)
        return _calibrate.calibrate_all(node, ctx.session.network,
                                        ctx.reporter)

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
        'calibrate', help='calibrate (the motor turns)',
        description='With no STEP: the full sequence (encoder test, current '
        'bias, gain, sense slope, encoder zero, baseline fold). Steps: ' +
        '; '.join('{}: {}'.format(k, v[0]) for k, v in _calibrate.STEPS.items()))
    p.set_defaults(func=_cmd_calibrate, needs_bus=True)
    p.add_argument('steps', nargs='*', metavar='STEP',
                   help='any of ' + ', '.join(_calibrate.STEPS))
    p.add_argument('--quick', action='store_true',
                   help='the quick full sequence (coarser encoder zero, '
                   '4-level slope fit)')
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
