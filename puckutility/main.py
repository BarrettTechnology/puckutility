"""main.py — the ``puckutility`` entry point: the GUI, or a command.

    puckutility                          the GUI
    puckutility [--can DEV] [--touchscreen]
                                         the GUI, on that port / fullscreen
    puckutility gui [...]                the GUI
    puckutility <command> [...]          the command line (--help)

The flat flags of puckutilityapp.py still work and are translated to
commands::

    --scan                       scan
    --info                       info (every puck unless --id)
    --flash FW                   flash FW
    --config CSV                 config load CSV
    --calibrate [--quick]        calibrate [--quick]
    --calibrate-settling         calibrate settling
    --calibrate-slope            calibrate slope
    --system-config INI          system-config INI
    --set-id NEW (--id OLD)      set-id NEW
    --flash-canable [FW]         flash-canable [FW]

with --can, --all and --verbose carried over, and ``--id 1 2 3`` running
the command once per puck, one after another.

``puckutility-gui`` (a gui_scripts entry, no console window on Windows)
always starts the GUI.
"""

import argparse
import sys

#: The old operation flags; any one of them means the old command line.
LEGACY_OPS = ('--scan', '--info', '--flash', '--config', '--calibrate',
              '--calibrate-settling', '--calibrate-slope', '--system-config',
              '--set-id', '--flash-canable')


def gui_main(argv=None):
    from .gui.app import main as run_gui
    return run_gui(argv)


def _legacy_parser():
    parser = argparse.ArgumentParser(
        prog='puckutility',
        description='The old puckutilityapp.py flags (see: puckutility --help '
                    'for the commands they map to).')
    parser.add_argument('--can', metavar='DEVICE')
    parser.add_argument('--id', type=int, nargs='+', metavar='ID')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--touchscreen', action='store_true')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    ops = parser.add_mutually_exclusive_group(required=True)
    ops.add_argument('--scan', action='store_true')
    ops.add_argument('--info', action='store_true')
    ops.add_argument('--flash', metavar='FIRMWARE')
    ops.add_argument('--config', metavar='CSV')
    ops.add_argument('--calibrate', action='store_true')
    ops.add_argument('--calibrate-settling', action='store_true',
                     dest='calibrate_settling')
    ops.add_argument('--calibrate-slope', action='store_true',
                     dest='calibrate_slope')
    ops.add_argument('--system-config', metavar='INI', dest='system_config')
    ops.add_argument('--set-id', metavar='NEW_ID', type=int, dest='set_id')
    ops.add_argument('--flash-canable', metavar='FIRMWARE', nargs='?',
                     const='', dest='flash_canable')
    return parser


def translate_legacy(argv):
    """The command lines (one per --id) the old flags *argv* mean."""
    parser = _legacy_parser()
    a = parser.parse_args(argv)
    common = []
    if a.can:
        common += ['--can', a.can]
    if a.verbose:
        common.append('--verbose')
    per_node = True             # honours --id / --all
    if a.scan:
        words, per_node = ['scan'], False
    elif a.info:
        words = ['info']
        if not a.id:            # the old --info showed every puck
            a.all = True
    elif a.flash:
        words = ['flash', a.flash]
    elif a.config:
        words = ['config', 'load', a.config]
    elif a.calibrate:
        words = ['calibrate'] + (['--quick'] if a.quick else [])
    elif a.calibrate_settling:
        words = ['calibrate', 'settling']
    elif a.calibrate_slope:
        words = ['calibrate', 'slope']
    elif a.system_config:
        words, per_node = ['system-config', a.system_config], False
    elif a.set_id is not None:
        if not a.id or len(a.id) != 1:
            parser.error('--set-id needs exactly one --id <current node ID>')
        words, per_node = ['set-id', str(a.set_id)], False
        common += ['--id', str(a.id[0])]
    else:
        words, per_node = ['flash-canable'], False
        if a.flash_canable:
            words.append(a.flash_canable)
    if not per_node:
        return [common + words]
    if a.all:
        return [common + words + ['--all']]
    if a.id:
        return [common + ['--id', str(n)] + words for n in a.id]
    return [common + words]


def _gui_only(argv):
    """True when *argv* holds nothing but the GUI's own flags."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == '--touchscreen' or token.startswith('--can='):
            i += 1
        elif token == '--can' and i + 1 < len(argv):
            i += 2
        else:
            return False
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or _gui_only(argv):
        return gui_main(argv)
    if argv[0] == 'gui':
        return gui_main(argv[1:])
    from .cli import main as run_cli
    if any(token in LEGACY_OPS for token in argv):
        code = 0
        for command in translate_legacy(argv):
            code = max(code, run_cli(command))
        return code
    return run_cli(argv)


if __name__ == '__main__':
    sys.exit(main())
