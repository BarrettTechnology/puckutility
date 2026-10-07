"""system_config.py — bring a whole system up to an INI's description.

Each INI section is one puck::

    [Shoulder]
    ID = 1
    CSV = P4-42-shoulder.csv        ; bare names are looked for in config/
    fw_version = 4.4.0              ; optional: flash when the puck differs
    fw = P4-4.4.0.ebin              ; bare names are looked for in firmware/

For each puck on the bus: flash when its firmware is not fw_version, load the
CSV (saved and rebooted), and then offer to calibrate everything configured.
"""

import configparser

from p4core import ops
from p4core.reporter import Reporter

from .. import paths


class SystemConfigError(RuntimeError):
    pass


def read_ini(path):
    """The pucks an INI describes: dicts of section, node_id, csv,
    fw_version and fw (paths resolved against config/ and firmware/)."""
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise SystemConfigError('cannot read {}'.format(path))
    pucks = []
    for section in cfg.sections():
        s = cfg[section]
        try:
            node_id = int(s['ID'])
            csv_path = paths._resolve_path(s['CSV'], paths.CONFIG_DIR)
        except (KeyError, ValueError) as exc:
            raise SystemConfigError('[{}] needs ID and CSV: {}'.format(
                section, exc))
        pucks.append(dict(
            section=section, node_id=node_id, csv=csv_path,
            fw_version=s.get('fw_version') or None,
            fw=paths._resolve_path(s.get('fw'), paths.FIRMWARE_DIR) or None))
    return pucks


def apply(session, ini_path, reporter=None, calibrate=None):
    """Flash and configure every puck in *ini_path* found on the bus.

    *calibrate(node_id)* runs for each configured puck when the user agrees
    (the default is no).  Returns {node_id: 'configured' | 'missing' |
    'flash failed' | 'config failed'}.
    """
    reporter = reporter if reporter is not None else Reporter()
    pucks = read_ini(ini_path)
    found = session.scan()
    reporter.note('Found {} node(s): {}'.format(len(found), found))
    outcome = {}
    for puck in pucks:
        node_id, section = puck['node_id'], puck['section']
        if node_id not in found:
            reporter.warn('Node {} ({}) not found on the bus, skipping'.format(
                node_id, section))
            outcome[node_id] = 'missing'
            continue
        reporter.status('Configuring node {} ({})'.format(node_id, section))
        if puck['fw_version'] and puck['fw']:
            version = session.read_version(node_id)
            if version != puck['fw_version']:
                reporter.note('Firmware {} -> {}'.format(
                    version, puck['fw_version']))
                try:
                    ops.flash(session, node_id, puck['fw'], reporter)
                except ops.OperationError as exc:
                    reporter.warn('{}; skipping the config for node {}'
                                  .format(exc, node_id))
                    outcome[node_id] = 'flash failed'
                    continue
            else:
                reporter.note('Firmware {} is up to date'.format(version))
        result = ops.load_config(session, node_id, puck['csv'], reporter)
        outcome[node_id] = 'configured' if result.ok else 'config failed'
    configured = [n for n, o in outcome.items() if o == 'configured']
    if configured and calibrate is not None and reporter.confirm(
            'Calibration is required after configuration. Calibrate {} now? '
            '(the motors turn)'.format(configured), default=False):
        for node_id in configured:
            reporter.status('Calibrating node {}'.format(node_id))
            calibrate(node_id)
    return outcome
