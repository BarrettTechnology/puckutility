"""csvaudit.py — check an A/B test CSV against its firmware's EDS.

    python csvaudit.py CSV EDS [--line legacy|current] [-v]

Every non-volatile, writable entry the firmware has is listed as one of
  present    the CSV writes it
  excluded   the CSV must not write it (per-actuator calibration, identity,
             node ID, PDO mappings) or need not (communication rows)
  MISSING    the CSV leaves it at whatever the puck had

and the CSV must also
  - set 0x21B2 = 0, 0x1F80 = 0, 0x1017 = 0, 0x1015 = 50 and 0x2406 = 0,
  - set every RPDO/TPDO mapping count (0x1600-3,0 / 0x1A00-3,0) to 0,
  - give each row the EDS's data type, REAL32 rows in float form,
  - write nothing the EDS does not have, and nothing read-only.

Exit status 1 on any MISSING row or error.  Rows that are writable but not
non-volatile (lost at reboot) are warnings.  The NV list comes from the
firmware's co_dict.c (p4core/data/nv), not from the EDS, which does not say.
"""

import argparse
import os
import sys

import canopen
from canopen.objectdictionary import ODVariable

from p4core import config_csv, nvjson
from p4core.config_csv import BOARD_CALIBRATION

#: The values every A/B CSV sets: termination off, boot to operational, no
#: heartbeat, 5 ms EMCY inhibit, no SYNC-loss fault (the A/B script runs SYNC
#: from the host and a stopped script must not fault the drive).
REQUIRED = {(0x21B2, 0): 0, (0x1F80, 0): 0, (0x1017, 0): 0, (0x1015, 0): 50,
            (0x2406, 0): 0}
PDO_COUNTS = [(i, 0) for i in (0x1600, 0x1601, 0x1602, 0x1603,
                               0x1A00, 0x1A01, 0x1A02, 0x1A03)]

#: Must not appear: per-actuator calibration, the identity object, the node
#: ID (the CSV is for any puck) and PDO mapping entries (the PDOs are empty;
#: ab-testing.py and testp4.py map their own).
FORBIDDEN_REASON = (
    (lambda k: k in BOARD_CALIBRATION, 'per-actuator calibration'),
    (lambda k: k == (0x3009, 9), 'per-actuator calibration (bias reference)'),
    (lambda k: k == (0x3001, 11), 'per-board bus-voltage trim'),
    (lambda k: k[0] == 0x1018, 'identity'),
    (lambda k: k == (0x21B0, 0), 'node ID'),
    (lambda k: 0x1600 <= k[0] < 0x1800 or 0x1A00 <= k[0] < 0x1C00 and k[1],
     'PDO mapping entry'),
)
#: Need not appear: communication parameters the defaults cover, the
#: firmware's own bookkeeping, and the encoder filter on 4.3.x, which that
#: firmware writes into the chip at every boot (it is in the CSV anyway).
OPTIONAL_REASON = (
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


def forbidden(key):
    if key in PDO_COUNTS:
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


def audit(csv_path, eds_path, line=None):
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
        if key in written:
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
        why = forbidden(key)
        if why:
            errors.append('{}: {} must not be in the CSV'.format(where, why))
        elif key not in nv and key not in VOLATILE_OK and 'w' in var.access_type:
            warnings.append('{}: not non-volatile, lost at reboot'.format(where))

    for key, want in list(REQUIRED.items()) + [(k, 0) for k in PDO_COUNTS]:
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

    present, excluded, missing = [], [], []
    for key in sorted(nv):
        var = eds.get(key)
        if var is None or 'w' not in var.access_type:
            continue
        item = (key, var.name)
        why = forbidden(key) or optional(key) or (
            'not used by this firmware' if key in UNUSED[line] else None)
        if key in written:
            present.append(item)
        elif why:
            excluded.append(item + (why,))
        else:
            missing.append(item)
    return dict(line=line, present=present, excluded=excluded, missing=missing,
                errors=errors, warnings=warnings)


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
    p.add_argument('-v', '--verbose', action='store_true',
                   help='list present and excluded rows too')
    args = p.parse_args(argv)
    result = audit(args.csv, args.eds, args.line)
    report(result, args.verbose)
    return 1 if result['missing'] or result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
