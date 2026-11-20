"""csvaudit.py — check a config CSV against its firmware's EDS.

    python csvaudit.py CSV EDS [--line legacy|current] [--pdos empty|standard] [-v]

Every non-volatile, writable entry the firmware has is listed as one of
  present    the CSV writes it
  excluded   the CSV must not write it (per-actuator calibration, identity
             other than the product code and revision, node ID, PDO
             mappings) or need not (communication rows)
  MISSING    the CSV leaves it at whatever the puck had

and the CSV must also
  - set 0x21B2 = 0, 0x1F80 = 4, 0x1017 = 0, 0x1015 = 50 and 0x2406 = 0,
  - with --pdos empty (the default, the A/B CSVs): set every RPDO/TPDO
    mapping count (0x1600-3,0 / 0x1A00-3,0) to 0 and map nothing;
    with --pdos standard: map RPDO1-2 and TPDO1-2 as p4core.pdo_defaults
    does (the DS402 set the tools use) and leave PDO 3 and 4 empty,
  - give each row the EDS's data type, REAL32 rows in float form,
  - on 5.x, once the velocity loop is tuned (0x2381,1 not 0), set the CSI
    gains 0x2383,1-2 too: at 0, cyclic sync impedance (-2) gives no torque,
  - write nothing the EDS does not have, and nothing read-only,
  - if it writes the product code 0x1018,2, give the code of the model its
    file name starts with (P4-32-... = 5760, P4-42-... = 5755).
A geared CSV (0x6091,1 / 0x6091,2 > 1) that leaves the settle integrator
0x2381,4 at 0 gets a warning: held positions stop short through the friction.

Exit status 1 on any MISSING row or error.  Rows that are writable but not
non-volatile (lost at reboot) are warnings.  The NV list comes from the
firmware's co_dict.c (p4core/data/nv), not from the EDS, which does not say.
"""

import argparse
import os
import re
import sys

import canopen
from canopen.objectdictionary import ODVariable

from p4core import config_csv, nvjson, pdo_defaults
from p4core.can_backend import PRODUCT_CODE_MODELS
from p4core.config_csv import BOARD_CALIBRATION

#: The values every A/B CSV sets: termination off, boot to PRE-OPERATIONAL
#: (the tools start the node themselves), no heartbeat, 5 ms EMCY inhibit,
#: no SYNC-loss fault (the A/B script runs SYNC from the host and a stopped
#: script must not fault the drive).
REQUIRED = {(0x21B2, 0): 0, (0x1F80, 0): 4, (0x1017, 0): 0, (0x1015, 0): 50,
            (0x2406, 0): 0}
#: The identity rows a CSV may set: product code and revision number.
IDENTITY_OK = frozenset([(0x1018, 2), (0x1018, 3)])
PDO_COUNTS = [(i, 0) for i in (0x1600, 0x1601, 0x1602, 0x1603,
                               0x1A00, 0x1A01, 0x1A02, 0x1A03)]

#: Must not appear: per-actuator calibration, the identity object but its
#: product code and revision (a CSV is for one model, so those two are the
#: model's; the serial and the end-user codes are the board's), the node ID
#: (the CSV is for any puck of the model) and PDO mapping entries (the PDOs
#: are empty; ab-testing.py and testp4.py map their own).
FORBIDDEN_REASON = (
    (lambda k: k in BOARD_CALIBRATION, 'per-actuator calibration'),
    (lambda k: k == (0x3009, 9), 'per-actuator calibration (bias reference)'),
    (lambda k: k == (0x3001, 11), 'per-board bus-voltage trim'),
    (lambda k: k[0] == 0x1018 and k not in IDENTITY_OK, 'identity'),
    (lambda k: k == (0x21B0, 0), 'node ID'),
    (lambda k: 0x1600 <= k[0] < 0x1800 or 0x1A00 <= k[0] < 0x1C00 and k[1],
     'PDO mapping entry'),
)
#: Need not appear: communication parameters the defaults cover, the
#: firmware's own bookkeeping, and the encoder filter on 4.3.x, which that
#: firmware writes into the chip at every boot (it is in the CSV anyway).
OPTIONAL_REASON = (
    (lambda k: k in IDENTITY_OK, 'identity (the model\'s, if given)'),
    (lambda k: 0x1000 <= k[0] < 0x2000, 'communication'),
    (lambda k: k[0] == 0x3005, 'firmware bookkeeping'),
    (lambda k: k == (0x3013, 7), 'obsolete, ignored by the firmware'),
)
#: NV rows the firmware never reads (checked against its source: no
#: parseGetValuePtr / BIND of them, or only commented out).  A CSV may leave
#: them out; the wipe leaves their defaults.  4.3.x steers the user polarity
#: from 0x3013,3, not 0x607E.  0x3024 (motor datasheet figures and old tuning
#: factors) is host-side only on both; the templates keep its datasheet rows.
_HOST_ONLY = [(0x3024, s) for s in range(1, 9)]
UNUSED = {
    'legacy': frozenset([(0x607E, 0), (0x2110, 0), (0x2111, 0), (0x2112, 0),
                         (0x2280, 1), (0x2280, 2), (0x2280, 3), (0x3001, 4)] +
                        [(0x3019, s) for s in (1, 2, 3, 4)] + _HOST_ONLY),
    'current': frozenset([(0x2280, 1), (0x2280, 2), (0x2280, 3)] +
                         [(0x3019, s) for s in (1, 2, 3, 4)] + _HOST_ONLY),
}
#: Writable, not non-volatile, but no warning: 5.x derives the rated torque
#: (written, it says what the CSV's per-mille rows were OF; config_csv) and
#: mirrors Kt into the 16-bit 0x3011,4.
VOLATILE_OK = frozenset([(0x6076, 0), (0x3011, 4)])


#: --pdos standard: {mapping index: [(index, sub), ...]} from p4core.pdo_defaults,
#: PDO 1 and 2 only (PDO 3 and 4 there are the tools' own RAM extras).
STANDARD_MAPPING = {}
for _n in (1, 2):
    STANDARD_MAPPING[0x1600 + _n - 1] = list(pdo_defaults.RPDOS[_n][2])
    STANDARD_MAPPING[0x1A00 + _n - 1] = list(pdo_defaults.TPDOS[_n][2])
PDOS = ('empty', 'standard')


def forbidden(key, pdos='empty'):
    if key in PDO_COUNTS:
        return None
    if pdos == 'standard' and key[0] in STANDARD_MAPPING and key[1]:
        return None
    for test, why in FORBIDDEN_REASON:
        if test(key):
            return why
    return None


def optional(key):
    for test, why in OPTIONAL_REASON:
        if test(key):
            return why
    return None


def eds_entries(od):
    """{(index, sub): ODVariable} for every variable in *od*."""
    out = {}
    for obj in od.values():
        if isinstance(obj, ODVariable):
            out[(obj.index, 0)] = obj
        else:
            for var in obj.values():
                out[(var.index, var.subindex)] = var
    return out


def _typename(var):
    for name, code in vars(canopen.objectdictionary.datatypes).items():
        if name.isupper() and code == var.data_type:
            return name
    return hex(var.data_type)


def is_float_text(text):
    """A REAL32 row's data written as a number (27, 0.138, 1e-5), not as
    the hex of its bits (0x3e585be2).  A trailing # comment is allowed."""
    text = text.split('#')[0].strip().lower()
    if text.lstrip('-+').startswith('0x'):
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def audit(csv_path, eds_path, line=None, pdos='empty'):
    """dict(present, excluded, missing, errors, warnings): lists of strings
    (present/excluded/missing are (key, name[, reason]) tuples)."""
    rows = config_csv.read(csv_path)
    od = canopen.import_od(eds_path)
    eds = eds_entries(od)
    if line is None:
        line = 'current' if (0x3003, 0) in eds else 'legacy'
    nv = set(nvjson.nv_keys(line))
    errors, warnings = [], []
    errs, warns = config_csv.validate(rows, node_id=1, od=od)
    errors += config_csv.describe(errs)
    warnings += config_csv.describe(warns)

    written = {}
    for row in rows:
        if row.kind != 'entry' or row.command:
            continue
        key = config_csv._key(row)
        if key is None:
            continue
        where = 'line {} ({:04X},{})'.format(row.line, *key)
        if key in written and not (pdos == 'standard' and key in PDO_COUNTS):
            # (a standard mapping writes its count 0, the entries, then the count)
            warnings.append('{}: also written on line {}'.format(where, written[key].line))
        written[key] = row
        var = eds.get(key)
        if var is None:
            errors.append('{}: not in the EDS'.format(where))
            continue
        if 'w' not in var.access_type:
            errors.append('{}: read-only ({})'.format(where, var.access_type))
        if row.datatype != _typename(var):
            errors.append('{}: type {} but the EDS says {}'.format(
                where, row.datatype, _typename(var)))
        if row.datatype == 'REAL32' and not is_float_text(row.data):
            errors.append('{}: REAL32 {!r} is not in float form'.format(where, row.data))
        why = forbidden(key, pdos)
        if why:
            errors.append('{}: {} must not be in the CSV'.format(where, why))
        elif key not in nv and key not in VOLATILE_OK and 'w' in var.access_type:
            warnings.append('{}: not non-volatile, lost at reboot'.format(where))

    counts = {k: len(STANDARD_MAPPING.get(k[0], [])) if pdos == 'standard' else 0
              for k in PDO_COUNTS}
    for key, want in list(REQUIRED.items()) + list(counts.items()):
        row = written.get(key)
        if key not in eds:
            continue
        if row is None:
            errors.append('{:04X},{} must be {} (not in the CSV)'.format(*key, want))
            continue
        try:
            got = config_csv.encode(row.datatype, row.data, 1)[1]
        except ValueError:
            continue
        if got != want:
            errors.append('line {} ({:04X},{}): {} but must be {}'.format(
                row.line, *key, row.data, want))

    if pdos == 'standard':
        errors += _standard_mapping(written, eds)
    errors += _product_code(written, csv_path)
    errors += _csi_gains(written)
    warnings += _settle_ki(written)

    present, excluded, missing = [], [], []
    for key in sorted(nv):
        var = eds.get(key)
        if var is None or 'w' not in var.access_type:
            continue
        item = (key, var.name)
        why = forbidden(key, pdos) or optional(key) or (
            'not used by this firmware' if key in UNUSED[line] else None)
        if key in written:
            present.append(item)
        elif why:
            excluded.append(item + (why,))
        else:
            missing.append(item)
    return dict(line=line, present=present, excluded=excluded, missing=missing,
                errors=errors, warnings=warnings)


def _value(row):
    try:
        return config_csv.encode(row.datatype, row.data, 1)[1]
    except (ValueError, TypeError):
        return None


def _standard_mapping(written, eds):
    """--pdos standard: the entries of PDO 1 and 2 are p4core.pdo_defaults'."""
    out = []
    for index, objects in sorted(STANDARD_MAPPING.items()):
        for sub, (oi, osub) in enumerate(objects, 1):
            want = (oi << 16) | (osub << 8) | _bits(eds[(oi, osub)])
            row = written.get((index, sub))
            if row is None:
                out.append('{:04X},{} must map {:04X},{} (not in the CSV)'.format(index, sub, oi, osub))
            elif _value(row) != want:
                out.append('line {} ({:04X},{}): {} but the standard mapping is 0x{:08x}'
                           .format(row.line, index, sub, row.data, want))
    return out


def _bits(var):
    """The PDO mapping length of a variable, in bits."""
    from canopen.objectdictionary import datatypes as dt
    sizes = {dt.BOOLEAN: 8, dt.INTEGER8: 8, dt.UNSIGNED8: 8, dt.INTEGER16: 16,
             dt.UNSIGNED16: 16, dt.INTEGER32: 32, dt.UNSIGNED32: 32, dt.REAL32: 32,
             dt.INTEGER64: 64, dt.UNSIGNED64: 64, dt.REAL64: 64}
    return sizes[var.data_type]


def _product_code(written, csv_path):
    """0x1018,2 must be a code of the model the file name starts with."""
    row = written.get((0x1018, 2))
    if row is None:
        return []
    where = 'line {} (1018,2)'.format(row.line)
    m = re.match(r'(P4-\d+)', os.path.basename(csv_path))
    if not m:
        return ['{}: product code in a CSV whose file name names no model '
                '(P4-32-..., P4-42-...)'.format(where)]
    model = m.group(1)
    codes = sorted(c for c, name in PRODUCT_CODE_MODELS.items() if name == model)
    try:
        got = config_csv.encode(row.datatype, row.data, 1)[1]
    except ValueError:
        return []
    if got not in codes:
        return ['{}: product code {} but a {} is {}'.format(
            where, row.data, model, ' or '.join(map(str, codes)) or 'not known')]
    return []


def _csi_gains(written):
    """The CSI gains must not be left at 0 in a tuned CSV.  The usual
    starting point is the node's own loop: kd = 0x2381,1 and
    kp = 0x2382,1 x 0x2381,1 (notes/impedance.html)."""
    vel = written.get((0x2381, 1))
    if vel is None or not _value(vel):
        return []
    out = []
    for sub, name in ((1, 'kp'), (2, 'kd')):
        row = written.get((0x2383, sub))
        if row is not None and not _value(row):
            out.append('line {} (2383,{}): CSI {} is 0 but the loop is tuned; mode -2 '
                       'gives no torque (start from kd = velocity Kp 0x2381,1, kp = position Kp 0x2382,1 x velocity Kp)'
                       .format(row.line, sub, name))
    return out


def _settle_ki(written):
    """A warning when a geared actuator's CSV leaves the settle integrator
    0x2381,4 at 0: through the gearhead's friction the position stops short
    of a held set-point by the stiction band and stays there (the plateau
    error of ab_testing.py's square runs).  Not an error: 0 is valid, and
    a direct-drive motor (ratio 1) needs none."""
    ki = written.get((0x2381, 4))
    motor, shaft = written.get((0x6091, 1)), written.get((0x6091, 2))
    if ki is None or motor is None or shaft is None or _value(ki):
        return []
    m, s = _value(motor), _value(shaft)
    if not m or not s or m / s <= 1:
        return []
    return ['line {} (2381,4): settle ki is 0 on a {:g}:1 gearhead; a held position '
            'stops short by the friction band'
            .format(ki.line, m / s)]


def report(result, verbose=False, out=None):
    out = out or sys.stdout
    def key(k):
        return '{:04X},{}'.format(*k)
    print('firmware line: {}'.format(result['line']), file=out)
    if verbose:
        for k, name in result['present']:
            print('  present   {:9} {}'.format(key(k), name), file=out)
        mapping = 0
        for k, name, why in result['excluded']:
            if why == 'PDO mapping entry':
                mapping += 1
                continue
            print('  excluded  {:9} {} ({})'.format(key(k), name, why), file=out)
        if mapping:
            print('  excluded  {} PDO mapping entries'.format(mapping), file=out)
    for k, name in result['missing']:
        print('  MISSING   {:9} {}'.format(key(k), name), file=out)
    for text in result['warnings']:
        print('  warning: ' + text, file=out)
    for text in result['errors']:
        print('  ERROR: ' + text, file=out)
    print('{} present, {} excluded, {} missing, {} errors, {} warnings'.format(
        len(result['present']), len(result['excluded']), len(result['missing']),
        len(result['errors']), len(result['warnings'])), file=out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('csv')
    p.add_argument('eds')
    p.add_argument('--line', choices=('legacy', 'current'),
                   help='firmware line (default: from the EDS: 0x3003 = current)')
    p.add_argument('--pdos', choices=PDOS, default='empty',
                   help='empty: no PDO mapped (the A/B CSVs, the default); standard: '
                        'RPDO1-2/TPDO1-2 as p4core.pdo_defaults maps them')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='list present and excluded rows too')
    args = p.parse_args(argv)
    result = audit(args.csv, args.eds, args.line, args.pdos)
    report(result, args.verbose)
    return 1 if result['missing'] or result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
