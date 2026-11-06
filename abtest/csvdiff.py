"""csvdiff.py — compare a configuration CSV with a read-back of the puck.

    python csvdiff.py CSV READBACK [--node-id N]
    python csvdiff.py CSV --can PORT --id N

READBACK is a CSV with the same rows holding what the puck has.  With
--can/--id the puck is read directly instead, every row by SDO.  Use that
after a load: `config export --template` keeps the template's value for
the communication rows (0x1xxx) and does not read them.  Every entry row of
CSV is compared by object (index, sub) and encoded value, so 0x10 and 16,
or 0.5 and 5e-1, are equal.  A row the read-back lacks, or the puck will
not read, is listed as missing.  Commands (FILE_VERSION and the like) and
comments are ignored.

Exit status 1 on any difference or missing row.
"""

import argparse
import struct
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


def diff_puck(csv_path, node, node_id=None):
    """diff() against *node* read by SDO.  The puck's bytes are cut or
    padded to the CSV value's width, so a U8 row reads as one byte."""
    from canopen.sdo.exceptions import SdoAbortedError, SdoCommunicationError
    node_id = node.id if node_id is None else node_id
    differ, missing = [], []
    for key, (row, want) in values(csv_path, node_id).items():
        try:
            raw = bytes(node.sdo.upload(*key))
        except (SdoAbortedError, SdoCommunicationError) as exc:
            missing.append((key, '{} ({})'.format(row.comment, exc)))
            continue
        if isinstance(want, bytes):
            got = (raw + bytes(len(want)))[:len(want)]
            same = got == want
            shown = got
        else:
            shown = raw.rstrip(b'\0').decode('latin-1')
            same = shown == want
        if not same:
            try:
                shown = config_csv._format(row.index, row.datatype,
                                           config_csv.decode(row.datatype, shown))
            except (TypeError, ValueError, struct.error):
                shown = shown.hex() if isinstance(shown, bytes) else shown
            differ.append((key, row.comment, row.data, shown))
    return differ, missing


def _read_puck(args):
    import canopen
    from p4core.can_backend import make_network
    network = make_network(args.can)
    try:
        node = network.add_node(args.id, canopen.ObjectDictionary())
        return diff_puck(args.csv, node, args.node_id)
    finally:
        network.disconnect()


def main(argv=None, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('csv')
    p.add_argument('readback', nargs='?')
    p.add_argument('--can', help='read the puck on this CAN port instead of READBACK')
    p.add_argument('--id', type=int, help='the puck to read (with --can)')
    p.add_argument('--node-id', type=int, help='for $ID rows (default: --id, or 1)')
    args = p.parse_args(argv)
    if (args.readback is None) == (args.can is None) or (args.can and args.id is None):
        p.error('give READBACK, or --can and --id')
    if args.can:
        differ, missing = _read_puck(args)
    else:
        differ, missing = diff(args.csv, args.readback, args.node_id or 1)
    for key, name, a, b in differ:
        print('  {:04X},{:<3} {:<40} CSV {}  puck {}'.format(*key, name[:40], a, b), file=out)
    for key, name in missing:
        print('  {:04X},{:<3} {} -- not in the read-back'.format(*key, name), file=out)
    print('{} rows differ, {} missing'.format(len(differ), len(missing)), file=out)
    return 1 if differ or missing else 0


if __name__ == '__main__':
    sys.exit(main())
