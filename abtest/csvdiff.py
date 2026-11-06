"""csvdiff.py — compare a configuration CSV with a read-back of the puck.

    python csvdiff.py CSV READBACK [--node-id N]

READBACK is `config export --template CSV READBACK` run after the CSV was
loaded: the same rows, filled with what the puck holds.  Every entry row of
CSV is compared by object (index, sub) and encoded value, so 0x10 and 16,
or 0.5 and 5e-1, are equal.  A row the read-back lacks is listed as
missing.  Commands (FILE_VERSION and the like) and comments are ignored.

Exit status 1 on any difference or missing row.
"""

import argparse
import sys

from p4core import config_csv


def values(path, node_id=1):
    """{(index, sub): (row, encoded bytes or text)} for the entry rows."""
    out = {}
    for row in config_csv.read(path):
        if row.kind != 'entry' or row.command:
            continue
        key = config_csv._key(row)
        if key is None:
            continue
        out[key] = (row, config_csv.encode(row.datatype, row.data, node_id)[0])
    return out


def diff(csv_path, readback_path, node_id=1):
    """(differ, missing): differ is [(key, name, csv data, read-back data)],
    missing [(key, name)], in the CSV's row order."""
    want, got = values(csv_path, node_id), values(readback_path, node_id)
    differ, missing = [], []
    for key, (row, raw) in want.items():
        if key not in got:
            missing.append((key, row.comment))
        elif got[key][1] != raw:
            differ.append((key, row.comment, row.data, got[key][0].data))
    return differ, missing


def main(argv=None, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('csv')
    p.add_argument('readback')
    p.add_argument('--node-id', type=int, default=1, help='for $ID rows (default 1)')
    args = p.parse_args(argv)
    differ, missing = diff(args.csv, args.readback, args.node_id)
    for key, name, a, b in differ:
        print('  {:04X},{:<3} {:<40} CSV {}  puck {}'.format(*key, name[:40], a, b), file=out)
    for key, name in missing:
        print('  {:04X},{:<3} {:<40} not in the read-back'.format(*key, name[:40]), file=out)
    print('{} rows differ, {} missing'.format(len(differ), len(missing)), file=out)
    return 1 if differ or missing else 0


if __name__ == '__main__':
    sys.exit(main())
