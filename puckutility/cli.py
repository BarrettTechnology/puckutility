"""cli.py — puckutility's command line.

    puckutility scan
    puckutility info [--all] [--no-flashloader]
    puckutility flash FIRMWARE [--all]
    puckutility config load CSV [--all]       (and export/check/templates)
    puckutility calibrate [STEP ...] [--no-save] [--all]
    puckutility version / nv save FILE / nv restore FILE
    puckutility system-config INI
    puckutility set-id NEW_ID
    puckutility reset [--all]
    puckutility flash-canable [FIRMWARE]

scan, flash, config, set-id and reset are p4core's; calibrate runs the
actuator calibration the GUI runs (controllers.calibrate, p4core.actuator's steps).  main.py turns the old
flat flags (--flash FW, --calibrate, ...) into these.
"""

import argparse
import sys

from p4core import can_backend, cli, firmware, ops

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
        fw = firmware.detect(node, refresh=True)
        extra = dict(settling_ns=_read(node, 'Amp', 'MaxSettlingTime'),
                     encoder_comp=_read(node, 0x3027, 1) if fw.has('runout') else None,
                     firmware_line='4.3.x' if fw.legacy else 'current')
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
            'not in this firmware' if r['firmware_line'] == '4.3.x'
            else 'unknown' if r['encoder_comp'] is None
            else 'ON' if r['encoder_comp'] else 'OFF'))
    ctx.emit(results if ctx.args.all else results[0], lines)


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------
def _reboot(ctx, node_id):
    """The reboot an encoder filter change needs: NMT reset, wait for the
    application, the node again (p4core Session.reset_node)."""
    return lambda: ctx.session.reset_node(node_id)


def _calibrate_node(ctx, node_id, steps=None, autosave=True):
    """The actuator calibration on *node_id*, its summary printed.  Each step
    saves to NV as it passes, unless *autosave* is False.  Returns (ok,
    session): ok when every step passed."""
    node = ctx.session.add_node(node_id)
    ok, session = _calibrate.calibrate_actuator(
        node, ctx.reporter, steps=steps, reboot=_reboot(ctx, node_id),
        network=ctx.session.network, autosave=autosave)
    ctx.reporter.note('node {}:\n{}'.format(
        node_id, _calibrate.summary_text(session, steps)))
    if session.unsaved and not autosave:
        ctx.reporter.note('node {}: the results are in RAM only (--no-save): a '
                          'reboot loses them'.format(node_id))
    elif session.unsaved:
        ctx.reporter.warn('node {}: the last results did not reach NV: they '
                          'are in RAM only'.format(node_id))
    elif autosave:
        ctx.reporter.note('node {}: the steps that passed are saved to NV'
                          .format(node_id))
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
            'Calibration turns the motor, continuously for cogging. Is the '
            'output free to turn?', default=True):
        return cli.EXIT_FAILED
    ok = True
    for node_id in cli.targets(ctx):
        ctx.reporter.status('--- Node {} ---'.format(node_id))
        if not _calibrate_node(ctx, node_id, steps or None,
                               autosave=not ctx.args.no_save)[0]:
            ctx.reporter.warn('node {}: calibration failed'.format(node_id))
            ok = False
    return cli.EXIT_OK if ok else cli.EXIT_FAILED


# ---------------------------------------------------------------------------
# system-config
# ---------------------------------------------------------------------------
def _cmd_system_config(ctx):
    from .controllers import system_config

    def calibrate(node_id):
        return _calibrate_node(ctx, node_id)[0]

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
        'puckutility v{}: flash, configure and calibrate P4 pucks. With no '
        'command, starts the GUI.'.format(__version__),
        version='v' + __version__)
    sub.add_parser('gui', help='start the GUI (the default)')
    cli.add_common_commands(
        sub, only=('scan', 'version', 'nv', 'flash', 'config', 'set-id', 'reset',
                   'wipe-nvm'))

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
        'order; named steps run in that order too. Each step saves to NV as it '
        'passes, unless --no-save. Steps: ' +
        '; '.join('{}: {}'.format(k, t) for k, t in _calibrate.STEPS.items()) +
        '. Also: offsets (bias), gainfactor (gain), encoder-offset or ezero '
        '(winding: encoder zero, electrical zero and winding order are one '
        'sweep), and ab for bias, gain and winding.')
    p.set_defaults(func=_cmd_calibrate, needs_bus=True)
    p.add_argument('steps', nargs='*', metavar='STEP', type=_step_name,
                   help='any of ' + ', '.join(_calibrate.STEPS) +
                   ', offsets, gainfactor, encoder-offset, or ab')
    p.add_argument('--no-save', action='store_true',
                   help='leave the winding, runout, friction and cogging '
                   'results in RAM '
                   'instead of saving each step to NV (the board calibrations '
                   'and the encoder filter still save themselves)')
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
