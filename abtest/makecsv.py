"""makecsv.py — fill an A/B template with values, without a puck.

    python makecsv.py TEMPLATE OUTPUT [--json NV.json] [--csv OLD.csv]...
                      [--set 0x3013,6=0x90]... [--version 1.0.0]

The values come, last one winning, from the template itself, then each
--json (an `nv save` file) and --csv (another configuration CSV) in the
order given, then --set.  The rows and their order are the template's: a
source row the template does not have is dropped (PDO mappings, per-actuator
calibration), and a template row no source has keeps the template's value
(reported).  Data are written in the template's types and style, as
`config export --template` writes them from a puck -- the puck route for a
node on the bench.  Run csvaudit.py on the result.
"""

import argparse
import sys

from canopen.sdo.exceptions import SdoAbortedError

from p4core import config_csv, nvjson


class ValueNode:
    """Just enough of a canopen node for config_csv.build_from_template:
    sdo.upload answers from a dict of raw bytes."""

    def __init__(self, values):
        self.values = values
        self.sdo = self

    def upload(self, index, sub):
        try:
            return self.values[(index, sub)]
        except KeyError:
            raise SdoAbortedError(0x06020000) from None


def _row_values(rows, node_id=1):
    out = {}
    for row in rows:
        key = config_csv._key(row) if row.kind == 'entry' and not row.command else None
        if key is None:
            continue
        raw, _ = config_csv.encode(row.datatype, row.data, node_id)
        if isinstance(raw, bytes):
            out[key] = raw
    return out


def parse_set(text):
    """'0x3013,6=0x90' -> ((0x3013, 6), '0x90')."""
    key, _, data = text.partition('=')
    index, _, sub = key.partition(',')
    if not data or not sub:
        raise argparse.ArgumentTypeError('expected INDEX,SUB=VALUE: {!r}'.format(text))
    return (int(index, 0), int(sub, 0)), data.strip()


def make(template, output, sources=(), sets=(), version=None, date=None, out=None):
    """Write *output*.  *sources* are ('json'|'csv', path) pairs; *sets*
    (key, data text) pairs, encoded in the template's type for that row.
    Returns the template rows no source supplied, as keys."""
    out = out or sys.stdout
    rows = config_csv.read(template)
    types = {config_csv._key(r): r.datatype for r in rows
             if r.kind == 'entry' and not r.command and config_csv._key(r)}
    values = _row_values(rows)
    supplied = set()
    for kind, path in sources:
        if kind == 'json':
            got = {nvjson.parse_key(k): bytes.fromhex(v)
                   for k, v in nvjson.load(path)['entries'].items()}
        else:
            got = _row_values(config_csv.read(path))
        for key, raw in got.items():
            if key in types:
                width = len(values.get(key, raw))
                values[key] = (raw + bytes(width))[:width] if config_csv.is_integer_type(
                    types[key]) or types[key] == 'REAL32' else raw
                supplied.add(key)
    for key, data in sets:
        if key not in types:
            raise ValueError('{:04X},{} is not in the template'.format(*key))
        values[key] = config_csv.encode(types[key], data, 1)[0]
        supplied.add(key)
    config_csv.build_from_template(ValueNode(values), template, output, date=date)
    if version:
        _stamp_version(output, version, date)
    kept = sorted(k for k in types if k not in supplied and not 0x1000 <= k[0] < 0x2000)
    for key in kept:
        print('template value kept: {:04X},{}'.format(*key), file=out)
    return kept


def _stamp_version(path, version, date):
    import datetime
    date = date or datetime.date.today().isoformat()
    with open(path) as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith('Version,FILE_VERSION,'):
            lines[i] = 'Version,FILE_VERSION,,,STRING,V{} ({})'.format(version, date)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('template')
    p.add_argument('output')
    p.add_argument('--json', action='append', default=[], metavar='NV.json')
    p.add_argument('--csv', action='append', default=[], metavar='CSV')
    p.add_argument('--set', action='append', default=[], type=parse_set,
                   metavar='INDEX,SUB=VALUE')
    p.add_argument('--version', help='FILE_VERSION, e.g. 1.0.0')
    args = p.parse_args(argv)
    # keep the command line's order between --json and --csv
    order = []
    argv = sys.argv[1:] if argv is None else argv
    js, cs = iter(args.json), iter(args.csv)
    for a in argv:
        if a == '--json' or a.startswith('--json='):
            order.append(('json', next(js)))
        elif a == '--csv' or a.startswith('--csv='):
            order.append(('csv', next(cs)))
    make(args.template, args.output, order, args.set, args.version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
