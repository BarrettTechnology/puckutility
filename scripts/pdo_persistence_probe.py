#!/usr/bin/env python3
"""
PDO COB-ID probe  --  "all the information" for the Set-ID PDO-remap fix.

Background
----------
puckutility's Set-ID button writes NetCfg + NMT-resets, changing the node ID. The
config CSV templates the 8 PDO COB-IDs as e.g. `0x200 | $ID`, so the concern is that
a bare Set-ID leaves them pointing at the OLD id. Before writing a host-side remap fix
we must know how THIS firmware actually handles PDO COB-IDs. This tool reports, in one
pass:

  Phase 0  Is the node running application firmware, or the flashloader?
  Phase 1  Do the standard PDO comm-param objects (0x1400-0x1403 / 0x1800-0x1803)
           even exist, and are they SDO-readable?
  Phase 2  GROUND TRUTH: what COB-ID does the puck ACTUALLY transmit TPDOs on, and
           does it track the node ID?  (Answers the real question even when the OD
           objects don't exist -- i.e. when COB-IDs are firmware-derived.)
  Phase 3  (only if the OD objects exist & are writable) Persistence sub-test:
           does a COB-ID write survive an NMT reset / power-cycle, with/without an
           explicit Save (0x1010:1)?  Interactive -- prompts you to power-cycle.

Safe/reversible: Phase 2 keeps the motor OFF (NMT operational only; no drive enable).
Phase 3 perturbs ONLY TPDO4 (0x1803:1) and always restores it in a finally block.

Usage:  ./scripts/pdo_persistence_probe.py [--node N] [--iface can0] [--persist]
        (--persist runs the interactive Phase-3 power-cycle sub-test)
"""
import argparse
import socket
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit('/scripts/', 1)[0])
import can_backend  # noqa: E402

RPDO = {1: (0x1400, 0x200), 2: (0x1401, 0x300), 3: (0x1402, 0x400), 4: (0x1403, 0x500)}
TPDO = {1: (0x1800, 0x180), 2: (0x1801, 0x280), 3: (0x1802, 0x380), 4: (0x1803, 0x480)}

SCRATCH_INDEX, SCRATCH_SUB, SCRATCH_BASE = 0x1803, 1, 0x480
SENTINEL_NODE = 0x33
SAVE_SIGNATURE = 0x65766173
VALID_BIT = 0x80000000
ABORT_NO_OBJECT = 0x06020000


def abort_code(e):
    return getattr(e, 'code', None)


def decode(val):
    cob = val & 0x7FF
    return "0x{:08X}  cob=0x{:03X} node={:3d} func=0x{:03X} valid={}".format(
        val, cob, val & 0x7F, val & 0x780, 'NO' if val & VALID_BIT else 'yes')


def rd(node, i, s):
    return int.from_bytes(node.sdo.upload(i, s), 'little', signed=False)


def wr(node, i, s, v):
    try:
        node.sdo.download(i, s, int(v).to_bytes(4, 'little', signed=False)); return None
    except Exception as e:
        return abort_code(e) or str(e)


def wait_for_node(node, timeout=8.0):
    import canopen
    saved = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 0.2
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                node.sdo.upload(0x1000, 0); return True
            except Exception:
                time.sleep(0.1)
        return False
    finally:
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = saved


def prompt(msg):
    try:
        input("\n>>> {} -- press Enter when ready...".format(msg))
    except EOFError:
        print("\n(no TTY -- run this directly in a terminal for the power-cycle steps)")
        raise SystemExit(2)


def is_flashloader(node):
    """The app's own discriminator: SetModeOfOperation (0x6060) exists only in the
    application firmware; the flashloader aborts it (object does not exist)."""
    try:
        node.sdo.upload(0x6060, 0); return False
    except Exception as e:
        return abort_code(e) == ABORT_NO_OBJECT


def phase1_objects(node, node_id):
    print("\n[Phase 1] PDO comm-param objects (expect cob = base | node_id):")
    present = {}
    for name, table in (('RPDO', RPDO), ('TPDO', TPDO)):
        for n, (index, base) in table.items():
            try:
                val = rd(node, index, SCRATCH_SUB)
                present[index] = val
                expect = base | (node_id & 0x7F)
                tag = 'in-sync' if (val & 0x7FF) == expect else \
                    '** STALE (expect 0x{:03X}) **'.format(expect)
                print("  {}{} @0x{:04X}:1  {}  {}".format(name, n, index, decode(val), tag))
            except Exception as e:
                c = abort_code(e)
                print("  {}{} @0x{:04X}:1  ABORT {}".format(
                    name, n, index, 'object-does-not-exist' if c == ABORT_NO_OBJECT else hex(c or 0)))
    if not present:
        print("  => firmware exposes NO standard PDO comm-param objects via SDO.")
        print("     COB-IDs are therefore firmware-managed (see Phase 2 ground truth);")
        print("     a host-side OD remap is not applicable to this firmware.")
    return present


def phase2_sniff(net, node, node_id):
    """Elicit TPDOs (NMT operational + SYNC bursts; motor stays off) and report the
    ACTUAL transmit COB-IDs vs node id -- the ground truth for 'do PDOs follow the id'."""
    print("\n[Phase 2] GROUND TRUTH -- sniffing actual TPDO COB-IDs (motor stays off)...")
    # best-effort: force IDLE mode if the object exists, so no torque is applied
    try:
        node.sdo.download(0x6060, 0, (0).to_bytes(1, 'little', signed=True))
    except Exception:
        pass
    s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.bind(('can0',)); s.settimeout(0.05)
    seen = {}
    net.send_message(0x0, [0x01, node_id])          # NMT start -> operational
    time.sleep(0.1)
    t0 = time.time()
    for _ in range(20):
        net.send_message(0x80, [])                  # SYNC
        while time.time() - t0 < 0.03:
            try:
                f = s.recv(16)
            except socket.timeout:
                break
            cid = struct.unpack('<I', f[:4])[0] & 0x7FF
            seen[cid] = seen.get(cid, 0) + 1
        t0 = time.time()
    net.send_message(0x0, [0x80, node_id])          # NMT back to pre-operational
    s.close()
    # drop the frames WE generated (SYNC 0x80, NMT 0x00)
    for k in (0x80, 0x00):
        seen.pop(k, None)
    if not seen:
        print("  no puck-originated frames observed. Either TPDOs are disabled, or this")
        print("  firmware doesn't transmit sync TPDOs. (Expected on the flashloader.)")
        return None
    print("  puck-originated COB-IDs:")
    verdict = 'unknown'
    for cid in sorted(seen):
        func, npart = cid & 0x780, cid & 0x7F
        label = {0x180: 'TPDO1', 0x280: 'TPDO2', 0x380: 'TPDO3', 0x480: 'TPDO4',
                 0x700: 'BOOT/HB', 0x580: 'SDOtx'}.get(func, '?')
        tag = ''
        if func in (0x180, 0x280, 0x380, 0x480):
            tag = '  node-field={} {}'.format(
                npart, 'TRACKS node id' if npart == node_id else '** != node id (STALE) **')
            verdict = 'tracks' if npart == node_id else 'stale'
        print("    0x{:03X}  x{:<3d}  {}{}".format(cid, seen[cid], label, tag))
    if verdict == 'tracks':
        print("  => TPDO COB-IDs TRACK the node id. If they auto-derive at boot, a Set-ID")
        print("     change already moves them and no host remap is needed -- confirm by")
        print("     re-running this after a node-id change.")
    elif verdict == 'stale':
        print("  => TPDO COB-IDs are STALE vs node id -- the remap fix IS needed.")
    return verdict


def phase3_persist(net, node, node_id):
    print("\n[Phase 3] Persistence sub-test on TPDO4 0x{:04X}:1...".format(SCRATCH_INDEX))
    orig = rd(node, SCRATCH_INDEX, SCRATCH_SUB)
    print("  original = {}".format(decode(orig)))
    sentinel = (orig & ~0x7F) | SENTINEL_NODE
    verdict = {}
    try:
        # Q2 direct vs disable-required
        err = wr(node, SCRATCH_INDEX, SCRATCH_SUB, sentinel)
        if err is None and (rd(node, SCRATCH_INDEX, SCRATCH_SUB) & 0x7F) == SENTINEL_NODE:
            verdict['write'] = 'direct write ACCEPTED'
        else:
            wr(node, SCRATCH_INDEX, SCRATCH_SUB, orig | VALID_BIT)
            wr(node, SCRATCH_INDEX, SCRATCH_SUB, sentinel | VALID_BIT)
            wr(node, SCRATCH_INDEX, SCRATCH_SUB, sentinel & ~VALID_BIT)
            verdict['write'] = ('REQUIRES disable(bit31) first'
                                if (rd(node, SCRATCH_INDEX, SCRATCH_SUB) & 0x7F) == SENTINEL_NODE
                                else 'FAILED ({})'.format(err))
        print("  [write] {}".format(verdict['write']))

        net.send_message(0x0, [0x81, node_id]); time.sleep(0.5); wait_for_node(node)
        got = rd(node, SCRATCH_INDEX, SCRATCH_SUB)
        verdict['nmt_reset'] = 'survived' if (got & 0x7F) == SENTINEL_NODE else 'reverted'
        print("  [NMT reset] {} ({})".format(verdict['nmt_reset'], decode(got)))

        prompt("POWER-CYCLE the puck now (NO save issued)")
        if not wait_for_node(node):
            print("  node did not reappear"); return verdict
        got = rd(node, SCRATCH_INDEX, SCRATCH_SUB)
        verdict['powercycle_nosave'] = ('PERSISTED (auto-save)'
                                        if (got & 0x7F) == SENTINEL_NODE else 'reverted')
        print("  [power-cycle, no save] {} ({})".format(verdict['powercycle_nosave'], decode(got)))

        if (got & 0x7F) != SENTINEL_NODE:
            wr(node, SCRATCH_INDEX, SCRATCH_SUB, sentinel)
            serr = wr(node, 0x1010, 1, SAVE_SIGNATURE)
            verdict['save_supported'] = 'aborted ({})'.format(serr) if serr else 'accepted'
            print("  [save 0x1010:1] {}".format(verdict['save_supported']))
            prompt("POWER-CYCLE the puck now (save WAS issued)")
            if wait_for_node(node):
                got = rd(node, SCRATCH_INDEX, SCRATCH_SUB)
                verdict['conclusion'] = ('explicit Save REQUIRED'
                                         if (got & 0x7F) == SENTINEL_NODE
                                         else 'firmware RE-DERIVES at boot (host cannot override)')
                print("  [power-cycle, saved] {} ({})".format(verdict['conclusion'], decode(got)))
        else:
            verdict['conclusion'] = 'auto-persist -- no save needed'
    finally:
        print("  restoring TPDO4 -> {}".format(decode(orig)))
        wr(node, SCRATCH_INDEX, SCRATCH_SUB, orig | VALID_BIT)
        wr(node, SCRATCH_INDEX, SCRATCH_SUB, orig)
        if verdict.get('save_supported', '').startswith('accepted'):
            wr(node, 0x1010, 1, SAVE_SIGNATURE)
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--node', type=int, default=None)
    ap.add_argument('--iface', default='can0')
    ap.add_argument('--persist', action='store_true',
                    help='run the interactive power-cycle persistence sub-test')
    args = ap.parse_args()

    r = can_backend.probe_interface(args.iface)
    if r[1] == 'in_use':
        print("Bus in use by another master -- close pucktuner/puckutility first."); return 1
    net = can_backend.make_network(args.iface, bitrate=1000000)
    try:
        node_id = args.node
        if node_id is None:
            net.scanner.reset(); net.scanner.search(); time.sleep(0.6)
            found = sorted(net.scanner.nodes)
            if len(found) != 1:
                print("Need exactly one node (or pass --node); found: {}".format(found)); return 1
            node_id = found[0]
        node = net.add_node(node_id, 'puck4.eds')
        print("node id = {}".format(node_id))

        # Phase 0 -- application firmware vs flashloader
        if is_flashloader(node):
            print("\n[Phase 0] This node is in FLASHLOADER mode (no application firmware:")
            print("  SetModeOfOperation/0x6060 aborts). There are no motor objects and no")
            print("  PDOs to test. Flash/boot the puck into application firmware, then re-run.")
            return 2
        print("[Phase 0] application firmware present.")

        present = phase1_objects(node, node_id)
        phase2_sniff(net, node, node_id)

        if args.persist:
            if SCRATCH_INDEX in present:
                v = phase3_persist(net, node, node_id)
                print("\n==================== PERSISTENCE VERDICT ====================")
                for k in ('write', 'nmt_reset', 'powercycle_nosave', 'save_supported', 'conclusion'):
                    if k in v:
                        print("  {:20s}: {}".format(k, v[k]))
                print("============================================================")
            else:
                print("\n[Phase 3] skipped: TPDO4 comm object not present, nothing to write/persist.")
        else:
            print("\n(Phase 3 persistence sub-test skipped -- pass --persist to run it.)")
        return 0
    finally:
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
