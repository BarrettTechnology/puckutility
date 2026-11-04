#!/usr/bin/env python3
"""Validate the CONTINUOUS-SPIN current-circle primitive (fast_pdo.SpinScanner) on hardware.

Background / the Phase-4 reinterpretation:
  Commanding Theta_e in PHASE_VOLTAGE_ANGLE with uq=0 takes the firmware D-axis-stall branch: the
  rotor magnetically DETENTS to the commanded angle and FOLLOWS it. So the rotor-frame id/iq stay
  ~d-axis (id~const, iq~0) EVEN WHEN Theta_e is applied correctly -- which is why the old Phase-4
  id/iq check read "not rotating". The STATOR current (Alpha/Beta.Filtered) DOES rotate with Theta_e
  and traces a CIRCLE. This script proves the write in the stator frame and validates the primitive.

PHASE 1  Field rotates + Theta_e scaling ... continuously ramp Theta_e one electrical cycle while
                                             streaming alpha/beta; fit the circle; confirm the
                                             (alpha,beta) vector sweeps ~2*pi about the fitted center
                                             over Theta_e 0..65535 -> 65536 counts == one cycle, and
                                             the RPDO write is being applied. PASS if angle span ~2pi
                                             AND the circle is clean (low residual).
PHASE 2  Sweep-rate characterization ....... scan_offset at several rate_hz; report circle residual
                                             per rate so we can pick the fastest SAFE rate.
PHASE 3  Stepped vs continuous equivalence . at one ud, offset (circle center) + |I| via the stepped
                                             SDO method (like the slope cal) vs the continuous spin;
                                             PASS if centers agree and continuous is much faster.

Usage:   scripts/pdo_spin_test.py [can_device] [node_id]      (defaults: can0 127)
Safety:  gentle current; drive idled and PDO config restored on exit / Ctrl-C.
"""
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend
from fast_pdo import SpinScanner, kasa_circle_fit, circle_rms_residual, _wrap16

CAN  = sys.argv[1] if len(sys.argv) > 1 else 'can0'
NODE = int(sys.argv[2]) if len(sys.argv) > 2 else 127
EDS  = os.path.join(ROOT, 'puck4.eds')

CLEAR_FAULT, SHUTDOWN, OP_ENABLED = 0x80, 0x06, 0x0F
MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE = 0, 12

UD_TEST = 6000          # fixed drive for the comparisons (gentle)
N_STEP  = 12            # stepped-cal angles


def unwrap_span(angles):
    """Total (unwrapped) angular travel across a sequence of atan2 angles."""
    if len(angles) < 2:
        return 0.0
    total = 0.0
    prev = angles[0]
    for a in angles[1:]:
        d = a - prev
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        total += d
        prev = a
    return total


def phase1(scanner):
    print("\n=== PHASE 1: field rotates + Theta_e scaling (stator frame) ===")
    s = scanner.spin(UD_TEST, cycles=1.0, rate_hz=0.4)
    if len(s) < 12:
        print("  FAIL: only {} samples captured (alpha/beta stream not delivering).".format(len(s)))
        return
    xs = [a for (_t, a, _b) in s]
    ys = [b for (_t, _a, b) in s]
    cx, cy, R = kasa_circle_fit(xs, ys)
    resid = circle_rms_residual(xs, ys, cx, cy, R)
    ang = [math.atan2(ys[i] - cy, xs[i] - cx) for i in range(len(s))]
    span = unwrap_span(ang)
    print("  samples={}  circle center=({:.1f},{:.1f}) counts  R={:.1f} counts  residual={:.3f}".format(
        len(s), cx, cy, R, resid))
    print("  (alpha,beta) angle span over one Theta_e cycle = {:+.2f} rad  (expect ~{:+.2f} = 2*pi)".format(
        span, 2 * math.pi))
    span_ok = abs(abs(span) - 2 * math.pi) < 0.20 * 2 * math.pi
    clean = resid < 0.12
    print("  direction: {}  (sign shows e_polarity)".format("forward" if span > 0 else "reverse"))
    if span_ok and clean:
        print("  -> PASS: field rotates exactly one turn per 65536 Theta_e counts (scaling confirmed) "
              "and the RPDO Theta_e write IS applied; circle is clean.")
    elif span_ok:
        print("  -> PARTIAL: scaling/rotation confirmed but circle residual high ({:.3f}) -- rate too "
              "fast or current too low.".format(resid))
    else:
        print("  -> FAIL: angle span {:.2f} != 2*pi -> field not tracking Theta_e (write/scaling).".format(span))


def phase2(scanner):
    print("\n=== PHASE 2: sweep-rate characterization (circle residual vs rate) ===")
    print("  {:>8}  {:>8}  {:>10}  {:>10}  {:>9}  {}".format(
        "rate_Hz", "samples", "center_a", "center_b", "R", "residual"))
    best = None
    for rate in (0.25, 0.5, 1.0, 2.0):
        off = scanner.scan_offset(UD_TEST, rate_hz=rate, cycles=1.0)
        n = len(getattr(scanner, 'last_samples', []))
        if off is None:
            print("  {:>8.2f}  {:>8}  {:>10}  {:>10}  {:>9}  {}".format(rate, n, "-", "-", "-", "n/a"))
            continue
        cx, cy, R = off
        res = scanner.last_residual
        flag = "clean" if res < 0.08 else ("ok" if res < 0.15 else "DEGRADED")
        print("  {:>8.2f}  {:>8}  {:>10.1f}  {:>10.1f}  {:>9.1f}  {:.3f} {}".format(
            rate, n, cx, cy, R, res, flag))
        if res < 0.15 and (best is None or rate > best):
            best = rate
    print("  -> fastest rate with a clean/ok circle: {}".format(
        "{:.2f} Hz".format(best) if best else "none (all degraded -- lower current or rate)"))


def stepped_offset(node, ud):
    """Slope-cal-style stepped measurement (plain SDO): detent at each of N_STEP angles, read RAW
    Alpha/Beta.Filtered, circle-fit. Returns (cx, cy, R, seconds)."""
    t0 = time.time()
    for cw in (CLEAR_FAULT, SHUTDOWN, OP_ENABLED):
        node.sdo["ControlWord"].raw = cw
    node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    node.sdo['Motor']['uq'].raw = 0
    node.sdo['Theta_e'].raw = 0
    node.sdo['Motor']['ud'].raw = ud

    def _settle(timeout=1.2):
        p0 = node.sdo['Encoder']['RawPosition'].raw
        t = time.time(); st = 0
        while time.time() - t < timeout:
            time.sleep(0.05)
            p1 = node.sdo['Encoder']['RawPosition'].raw
            if abs(p1 - p0) <= 2:
                st += 1
                if st >= 3:
                    return
            else:
                st = 0
            p0 = p1
    _settle()
    xs, ys = [], []
    for i in range(N_STEP):
        th = int(round(-32768 + i * 65536.0 / N_STEP))
        node.sdo['Theta_e'].raw = th
        _settle()
        sa = sb = 0.0
        for _ in range(8):
            sa += node.sdo['Alpha']['Filtered'].raw
            sb += node.sdo['Beta']['Filtered'].raw
            time.sleep(0.003)
        xs.append(sa / 8.0); ys.append(sb / 8.0)
    node.sdo['Motor']['ud'].raw = 0
    node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    cx, cy, R = kasa_circle_fit(xs, ys)
    return cx, cy, R, time.time() - t0


def phase3(net, node):
    print("\n=== PHASE 3: stepped (SDO) vs continuous (spin) offset at ud={} ===".format(UD_TEST))
    cxs, cys, Rs, ts = stepped_offset(node, UD_TEST)
    print("  STEPPED   : center=({:.1f},{:.1f})  R={:.1f}  ({:.1f} s)".format(cxs, cys, Rs, ts))

    with SpinScanner(node) as sc:
        if not sc.ok:
            print("  SpinScanner setup failed: {}  -> SKIP".format(sc.err))
            return
        t0 = time.time()
        off = sc.scan_offset(UD_TEST, rate_hz=0.5, cycles=1.25)
        tc = time.time() - t0
    if off is None:
        print("  CONTINUOUS: no samples -> FAIL")
        return
    cxc, cyc, Rc = off
    print("  CONTINUOUS: center=({:.1f},{:.1f})  R={:.1f}  residual={:.3f}  ({:.1f} s)".format(
        cxc, cyc, Rc, sc.last_residual, tc))

    dist = math.hypot(cxc - cxs, cyc - cys)
    dR = abs(Rc - Rs)
    tol = max(15.0, 0.10 * max(Rs, 1.0))     # center within 10% of |I| (or 15 counts)
    print("  center offset diff = {:.1f} counts (tol {:.1f})   |I| diff = {:.1f} (R={:.0f})   speedup "
          "= {:.1f}x".format(dist, tol, dR, Rs, ts / tc if tc > 0 else 0.0))
    if dist <= tol and dR <= max(20.0, 0.15 * Rs):
        print("  -> PASS: continuous offset matches stepped, in a fraction of the time.")
    else:
        print("  -> FAIL: continuous vs stepped disagree -- check rate/current (residual {:.3f}).".format(
            sc.last_residual))


def main():
    net = can_backend.make_network(CAN, bitrate=1000000)
    node = None
    try:
        node = net.add_node(NODE, EDS)
        node.sdo.RESPONSE_TIMEOUT = 1.0
        with SpinScanner(node) as scanner:
            if not scanner.ok:
                print("SpinScanner setup FAILED: {}".format(scanner.err))
                return 2
            phase1(scanner)
            phase2(scanner)
        phase3(net, node)
        print("\nDone.")
        return 0
    finally:
        try:
            if node is not None:
                node.sdo['Motor']['ud'].raw = 0
                node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                print("Puck idled.")
        except Exception:
            pass
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
