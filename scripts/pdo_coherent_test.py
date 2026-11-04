#!/usr/bin/env python3
"""ONE-RUN PDO diagnostic: pin why the TPDO id/iq `.raw` read FROZE, prove the raw-capture fix, and
prove the RPDO Theta_e/ud WRITE actually applies.

Everything below runs in a single hardware session and prints PASS/FAIL/counts per probe:

  PHASE 1  Config read-back .............. TPDO2/TPDO4 COB-ID / trans_type / enabled / mapping
  PHASE 2  SYNC-mechanism A/B ............ frames received on TPDO4 COB-ID under network.sync.start()
                                           vs a manual SYNC thread (is SYNC even reaching the puck?)
  PHASE 3  READ A/B across a current ramp  at each ud level, |I| via 3 paths side by side:
                                             - RAWCAP   : raw network callback + struct.unpack (the fix)
                                             - PDOMAP   : canopen node.tpdo[4].map[i].raw (old, frozen?)
                                             - SDO      : node.sdo reads (reference truth)
                                           plus: frames-on-wire delta, raw last-bytes, wait_for_reception
                                           timestamp advance, and a callbacks-INTACT vs CLEARED sub-test.
  PHASE 3c FastPDO end-to-end ............ drive FastPDO.read() exactly as calibrate_menu does.
  PHASE 4  RPDO WRITE probe .............. stream Theta_e+ud via RPDO3 (RPDO1 pre-armed async); does
                                           current flow and track the commanded angle? (vs SDO write)

Usage:   scripts/pdo_coherent_test.py [can_device] [node_id]      (defaults: can0 127)

Safety:  gentle current, drive idled (ud=0, IDLE) and TPDO map restored on exit / Ctrl-C.
"""
import math
import os
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend
from fast_pdo import FastPDO, RPDOWriter

CAN  = sys.argv[1] if len(sys.argv) > 1 else 'can0'
NODE = int(sys.argv[2]) if len(sys.argv) > 2 else 127
EDS  = os.path.join(ROOT, 'puck4.eds')

CLEAR_FAULT, SHUTDOWN, OP_ENABLED = 0x80, 0x06, 0x0F
MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE = 0, 12

UD_LEVELS = [2000, 4000, 6000, 8000, 10000]  # ramp to a REAL current (~300 mA needs ud ~10000)
UD_MAX    = 11000
SETTLE_S  = 0.20
MIN_SPAN  = 40.0        # raw-count span that counts as "moved"
REAL_SPAN = 100.0       # SDO |I| must sweep at least this (raw counts) to be a meaningful comparison


class RawCounter:
    """Plain network subscriber: counts frames on a COB-ID and decodes id/iq itself (no PdoMap)."""
    def __init__(self, fmt='<hh', nbytes=4):
        self.count = 0
        self.last = None
        self.last_bytes = None
        self.fmt = fmt
        self.nbytes = nbytes

    def __call__(self, can_id, data, timestamp):
        self.count += 1
        self.last_bytes = bytes(data)
        if len(data) >= self.nbytes:
            try:
                self.last = struct.unpack_from(self.fmt, data)
            except struct.error:
                pass


def energise(node):
    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo["ControlWord"].raw = cw
    node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    node.sdo['Theta_e'].raw = 0
    node.sdo['Motor']['ud'].raw = 0


def idle(node):
    try:
        node.sdo['Motor']['ud'].raw = 0
        node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    except Exception:
        pass


def sdo_idiq(node):
    return node.sdo['Motor']['id'].raw, node.sdo['CurrentFeedback'].raw


def mag(a, b):
    return math.hypot(a, b)


def span(vals):
    vals = [v for v in vals if v == v]     # drop NaN
    return (max(vals) - min(vals)) if vals else 0.0


# ---------------------------------------------------------------------------
def phase1_config(node):
    print("\n=== PHASE 1: PDO config read-back (app-default state) ===")
    try:
        node.tpdo.read()
    except Exception as e:
        print("  tpdo.read() failed:", e)
        return
    for i in (2, 4):
        try:
            tp = node.tpdo[i]
            mp = [(hex(v.index), v.subindex, v.length) for v in tp.map if v.length]
            print("  TPDO{}: cob=0x{:X} trans_type={} enabled={} map={}".format(
                i, tp.cob_id or 0, tp.trans_type, tp.enabled, mp))
        except Exception as e:
            print("  TPDO{}: read error {}".format(i, e))


# ---------------------------------------------------------------------------
def phase2_sync(net, node):
    print("\n=== PHASE 2: SYNC-mechanism A/B (frames on TPDO4 COB-ID over 0.5 s) ===")
    cob = (0x480 | (node.id & 0x7F))
    energise(node)
    node.sdo['Motor']['ud'].raw = 4000          # a little current so TPDO4 has changing content
    time.sleep(SETTLE_S)

    # A) canopen network.sync.start()
    ctr_a = RawCounter()
    net.subscribe(cob, ctr_a)
    try:
        node.network.sync.start(0.002)
        time.sleep(0.5)
        node.network.sync.stop()
    except Exception as e:
        print("  network.sync.start() error:", e)
    net.unsubscribe(cob, ctr_a)

    # B) manual SYNC thread (the PVCA way)
    ctr_b = RawCounter()
    net.subscribe(cob, ctr_b)
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                node.network.send_message(0x80, bytes())
            except Exception:
                return
            while (time.monotonic() - t0) < 0.002:
                pass
    th = threading.Thread(target=_loop, daemon=True)
    th.start()
    time.sleep(0.5)
    stop.set()
    th.join(timeout=0.5)
    net.unsubscribe(cob, ctr_b)

    idle(node)
    print("  A) network.sync.start(): frames={:4d}  last={}  {}".format(
        ctr_a.count, ctr_a.last, "PASS" if ctr_a.count > 50 else "FAIL (no stream)"))
    print("  B) manual SYNC thread  : frames={:4d}  last={}  {}".format(
        ctr_b.count, ctr_b.last, "PASS" if ctr_b.count > 50 else "FAIL (no stream)"))
    print("  -> SYNC reaches the puck via: {}".format(
        ", ".join(n for n, c in (("sync.start", ctr_a.count), ("manual", ctr_b.count)) if c > 50)
        or "NEITHER (SYNC/subscription problem)"))


# ---------------------------------------------------------------------------
def phase3_read(net, node, clear_callbacks):
    """Remap TPDO4->id+iq, then across a ramp compare RAWCAP vs PDOMAP vs SDO on the SAME frames."""
    tag = "callbacks CLEARED" if clear_callbacks else "callbacks INTACT"
    print("\n=== PHASE 3: READ A/B across ramp  ({}) ===".format(tag))
    cob = (0x480 | (node.id & 0x7F))
    rpdo1_cob = (0x200 | (node.id & 0x7F))

    # Remap TPDO4 = Motor.id + CurrentFeedback, trans_type=1, AND arm RPDO1 async (255) so SYNC can't
    # re-disable the drive during the read burst -- the coupling confirmed on hardware (PRE-OP).
    node.nmt.state = 'PRE-OPERATIONAL'
    node.tpdo.read()
    node.tpdo[4].clear()
    node.tpdo[4].add_variable('Motor', 'id')
    node.tpdo[4].add_variable('CurrentFeedback')
    node.tpdo[4].trans_type = 1
    node.tpdo[4].enabled = True
    try:
        node.sdo[0x1400][2].raw = 255                  # RPDO1 async (not applied every SYNC)
    except Exception as e:
        print("  (RPDO1 async set warning: {})".format(e))
    node.tpdo.save()
    node.tpdo.read()
    mp = [(hex(v.index), v.subindex, v.length) for v in node.tpdo[4].map if v.length]
    node.nmt.state = 'OPERATIONAL'
    print("  remapped TPDO4 map = {}  (expect id 0x3010:6 + CurrentFeedback 0x6078)".format(mp))

    saved_cbs = None
    if clear_callbacks:
        saved_cbs = list(node.tpdo[4].callbacks)
        node.tpdo[4].callbacks.clear()

    ctr = RawCounter()                        # raw wire capture (the fix's mechanism)
    net.subscribe(cob, ctr)

    energise(node)
    print("  {:>6}  {:>8}  {:>8}  {:>8}  {:>6}  {:>7}  {}".format(
        "ud", "RAWCAP", "PDOMAP", "SDO", "frames", "waitTS", "rawbytes"))
    rows = []
    last_ts = 0.0
    try:
        for ud in UD_LEVELS:
            ud = min(ud, UD_MAX)
            try:
                node.network.sync.stop()
            except Exception:
                pass
            node.sdo['Motor']['ud'].raw = ud
            time.sleep(SETTLE_S)
            s_id, s_iq = sdo_idiq(node)
            s_mag = mag(s_id, s_iq)

            c0 = ctr.count
            # Prime RPDO1 (async) with OP_ENABLED so SYNC keeps the drive on -> real current to read.
            node.network.send_message(rpdo1_cob, struct.pack('<HBh', OP_ENABLED, 12, 0))
            node.network.sync.start(0.002)
            ts = node.tpdo[4].wait_for_reception(0.2)     # PdoMap path timestamp
            time.sleep(0.05)
            frames = ctr.count - c0
            raw = ctr.last
            r_mag = mag(raw[0], raw[1]) if raw else float('nan')
            try:
                p_mag = mag(node.tpdo[4].map[0].raw, node.tpdo[4].map[1].raw)
            except Exception:
                p_mag = float('nan')
            node.network.sync.stop()

            ts_adv = "yes" if (ts and ts != last_ts) else "no"
            last_ts = ts or last_ts
            print("  {:>6}  {:>8.1f}  {:>8.1f}  {:>8.1f}  {:>6}  {:>7}  {}".format(
                ud, r_mag, p_mag, s_mag, frames, ts_adv,
                ctr.last_bytes.hex() if ctr.last_bytes else "-"))
            rows.append((r_mag, p_mag, s_mag, frames))
    finally:
        net.unsubscribe(cob, ctr)
        if saved_cbs is not None:
            node.tpdo[4].callbacks.extend(saved_cbs)
        idle(node)

    raw_span = span([r[0] for r in rows])
    pdo_span = span([r[1] for r in rows])
    sdo_span = span([r[2] for r in rows])
    frames_ok = all(r[3] > 0 for r in rows)
    # RAWCAP must land within tolerance of SDO at every level (it reads the SAME frames the SDO sees).
    tracks = all(abs(r[0] - r[2]) <= max(30.0, 0.25 * r[2]) for r in rows if r[0] == r[0])
    print("  spans:  RAWCAP={:.1f}  PDOMAP={:.1f}  SDO={:.1f}   frames_every_level={}  tracks={}".format(
        raw_span, pdo_span, sdo_span, frames_ok, tracks))
    if not frames_ok:
        print("  -> FAIL: frames did NOT arrive every level -> SYNC/subscription problem (not .raw).")
    elif sdo_span < REAL_SPAN:
        print("  -> INCONCLUSIVE: SDO |I| span {:.1f} < {:.0f} -> the drive did NOT hold real current "
              "during SYNC (RPDO1 not staying enabled). Fix the coupling before trusting the read."
              .format(sdo_span, REAL_SPAN))
    elif tracks and raw_span >= MIN_SPAN:
        note = "" if pdo_span >= MIN_SPAN else "  (PDOMAP .raw stayed frozen -- raw capture is the fix)"
        print("  -> PASS: drive held real current (SDO span {:.1f}); RAWCAP tracks SDO across the "
              "ramp.{}".format(sdo_span, note))
    elif raw_span < MIN_SPAN:
        print("  -> FAIL: frames arrive and current swept, but RAW bytes are FROZEN on the wire -> "
              "puck transmits stale TPDO4 with this 2-var map. Use split single-var TPDOs (see report).")
    else:
        print("  -> FAIL: RAWCAP did not track SDO within tolerance (see rows above).")


# ---------------------------------------------------------------------------
def phase3_fastpdo(node):
    """Sanity: drive FastPDO exactly as calibrate_menu does and confirm read() tracks a small ramp."""
    print("\n=== PHASE 3c: FastPDO.read() end-to-end (as the cal uses it) ===")
    fp = FastPDO(node)
    fp.__enter__()
    if not fp.ok:
        print("  FastPDO unavailable: {}  -> SKIP".format(fp.err))
        return
    energise(node)
    rows = []
    sdo_mags = []
    try:
        for ud in (4000, 7000, 10000):
            ud = min(ud, UD_MAX)
            fp.sync_off()
            node.sdo['Motor']['ud'].raw = ud
            time.sleep(SETTLE_S)
            s_id, s_iq = sdo_idiq(node)
            fp.sync_on()
            vals = [fp.read() for _ in range(6)]
            fp.sync_off()
            good = [v for v in vals if v]
            pm = (sum(mag(v[0], v[1]) for v in good) / len(good)) if good else float('nan')
            print("  ud={:5d}  FastPDO |I|={:7.1f}  SDO |I|={:7.1f}  reads_ok={}/{}".format(
                ud, pm, mag(s_id, s_iq), len(good), len(vals)))
            rows.append(pm)
            sdo_mags.append(mag(s_id, s_iq))
    finally:
        fp.__exit__(None, None, None)
        idle(node)
    spn = span(rows)
    sdo_spn = span(sdo_mags)
    if sdo_spn < REAL_SPAN:
        print("  FastPDO |I| span={:.1f}  SDO span={:.1f}  -> INCONCLUSIVE (drive didn't hold current)"
              .format(spn, sdo_spn))
    else:
        print("  FastPDO |I| span={:.1f}  SDO span={:.1f}  -> {}".format(
            spn, sdo_spn, "PASS (tracks real current)" if spn >= MIN_SPAN else "FAIL (frozen)"))


# ---------------------------------------------------------------------------
def phase4_write(net, node):
    """Prove RPDO3 Theta_e/ud writes APPLY: command several angles at fixed ud, watch current appear
    and rotate with the angle (via SDO). If the drive were being disabled every SYNC, |I| stays ~0."""
    print("\n=== PHASE 4: RPDO WRITE probe (Theta_e/ud via RPDO3, RPDO1 pre-armed async) ===")
    UD_CMD = 6000
    ANGLES = [0, 8192, 16384, 24576]          # quarter-turns in electrical angle (raw, 65536=2pi)
    w = RPDOWriter(node)
    w.__enter__()
    if not w.ok:
        print("  RPDOWriter setup failed: {}  -> SKIP".format(w.err))
        return
    started = False
    rows = []
    try:
        node.network.sync.start(0.002)        # run SYNC like real operation (tests drive stays on)
        started = True
        time.sleep(0.1)
        print("  {:>7}  {:>8}  {:>8}  {:>8}".format("theta_e", "SDO id", "SDO iq", "|I|"))
        for th in ANGLES:
            w.write(th, UD_CMD)               # RPDO3 frame; applied on receipt
            time.sleep(SETTLE_S)
            s_id, s_iq = sdo_idiq(node)
            m = mag(s_id, s_iq)
            print("  {:>7}  {:>8}  {:>8}  {:>8.1f}".format(th, s_id, s_iq, m))
            rows.append((th, s_id, s_iq, m))
        w.write(0, 0)
    finally:
        if started:
            try:
                node.network.sync.stop()
            except Exception:
                pass
        w.__exit__(None, None, None)
        idle(node)

    mags = [r[3] for r in rows]
    current_flows = any(m > 50 for m in mags)
    # NOTE: id/iq are the ROTOR frame. With uq=0 the firmware D-axis-stall branch detents the rotor to
    # Theta_e, so the rotor FOLLOWS the field and id/iq stay ~d-axis (id~const, iq~0) even though the
    # write IS applied. So id/iq NOT rotating here is EXPECTED, not a failure. The definitive proof
    # that Theta_e is applied is the STATOR frame (alpha/beta) tracing a circle -> scripts/pdo_spin_test.py.
    print("  current_flows={}  (id/iq are rotor-frame; they don't rotate when the rotor follows -- "
          "that's expected, see pdo_spin_test.py for the stator-frame circle proof)".format(current_flows))
    if current_flows:
        print("  -> PASS: RPDO writes APPLY (current flows on command). The open-loop 'didn't spin' "
              "was the unprimed RPDO1 disabling the drive every SYNC. Run pdo_spin_test.py to confirm "
              "the field rotates with Theta_e (stator frame).")
    else:
        print("  -> FAIL: no current -> drive not enabled. RPDO1 prime/async likely not taking; "
              "check firmware acceptance of the PVCA-style config.")


# ---------------------------------------------------------------------------
def main():
    net = can_backend.make_network(CAN, bitrate=1000000)
    node = None
    orig_rpdo1 = None
    try:
        node = net.add_node(NODE, EDS)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        try:
            orig_rpdo1 = node.sdo[0x1400][2].raw       # save RPDO1 trans_type to restore on exit
        except Exception:
            orig_rpdo1 = None
        phase1_config(node)
        phase2_sync(net, node)
        phase3_read(net, node, clear_callbacks=False)
        phase3_read(net, node, clear_callbacks=True)
        phase3_fastpdo(node)
        phase4_write(net, node)
        print("\nDone.")
        return 0
    finally:
        try:
            if node is not None:
                node.nmt.state = 'PRE-OPERATIONAL'
                node.tpdo.read()
                node.tpdo[4].clear()
                node.tpdo[4].add_variable('Motor', 'id')
                node.tpdo[4].trans_type = 1
                node.tpdo[4].enabled = True
                if orig_rpdo1 is not None:
                    try:
                        node.sdo[0x1400][2].raw = orig_rpdo1   # restore RPDO1 trans_type
                    except Exception:
                        pass
                node.tpdo.save()
                node.tpdo.read()
                node.nmt.state = 'OPERATIONAL'
                idle(node)
                print("Puck idled; TPDO4 restored to Motor.id-only; RPDO1 trans_type restored.")
        except Exception as e:
            print("(cleanup warning: {})".format(e))
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
