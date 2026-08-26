#!/usr/bin/env python3
"""tx_backpressure_test.py -- regression test for the "No Pucks Found" TX-buffer bug.

canopen's NodeScanner.search() blasts 127 SDO requests back-to-back, and
canopen's Network.send_message calls bus.send(msg) with NO timeout. python-can's
socketcan backend then falls back to timeout=0 and polls the socket with
select(..., 0), so the first moment of TX backpressure raises

    can.exceptions.CanOperationError: Transmit buffer full

The gs_usb / CandleLight TX URB pool is shallow enough that the scan burst
overruns it whenever the queue has not fully drained. puckutilityapp then
catches that error, matches "buffer" in str(e), and reports it to the user as
"No Pucks Found" -- sending you off to check power and CAN wiring that were
never the problem.

can_backend._tolerate_tx_backpressure() fixes it by giving sends a real timeout
so they WAIT for queue space instead of failing. This test drives repeated
back-to-back scans, which is what triggers the bug:

    stock code   -> scan 1 passes, scans 2..N raise "Transmit buffer full"
    with the fix -> all N scans pass and report a stable node set

Needs a live bus with at least one puck powered on.

    scripts/tx_backpressure_test.py --can can0 --iterations 5
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--can', default='can0', help='CAN interface / port (default can0)')
    ap.add_argument('--iterations', type=int, default=5,
                    help='back-to-back scans to run (default 5)')
    ap.add_argument('--settle', type=float, default=0.5,
                    help='seconds to wait for SDO replies after each scan (default 0.5)')
    ap.add_argument('--expect-nodes', type=int, default=None,
                    help='require exactly this many nodes on every scan')
    args = ap.parse_args()

    net = can_backend.make_network(args.can, bitrate=1_000_000)

    # The fix installs a send wrapper; flag its absence up front so a failure
    # further down is not mistaken for a hardware problem.
    wrapped = getattr(net.bus.send, '__name__', '') == '_send_waiting'
    print(f"bus            : {net.bus}")
    print(f"send wrapper   : {'installed' if wrapped else 'MISSING -- can_backend is unpatched'}")
    if not wrapped:
        print("                 (expect scans 2+ to fail with 'Transmit buffer full')")

    failures = []
    results = []
    try:
        for i in range(1, args.iterations + 1):
            net.scanner.reset()
            try:
                net.scanner.search()
            except Exception as e:
                failures.append((i, f"{type(e).__name__}: {e}"))
                print(f"  scan {i}: FAILED -> {type(e).__name__}: {e}")
                continue
            time.sleep(args.settle)
            nodes = sorted(net.scanner.nodes)
            results.append(nodes)
            print(f"  scan {i}: nodes = {nodes}")
    finally:
        try:
            net.disconnect()
        except Exception:
            pass

    print()
    if failures:
        print(f"FAIL: {len(failures)} of {args.iterations} scans raised an error")
        for i, err in failures:
            print(f"  scan {i}: {err}")
        return 1

    if not results or not results[0]:
        print("FAIL: no nodes found on any scan -- is a puck powered and on the bus?")
        return 1

    if any(n != results[0] for n in results):
        print(f"FAIL: node set was not stable across scans: {results}")
        return 1

    if args.expect_nodes is not None and len(results[0]) != args.expect_nodes:
        print(f"FAIL: expected {args.expect_nodes} nodes, found {len(results[0])}: {results[0]}")
        return 1

    print(f"PASS: {args.iterations}/{args.iterations} back-to-back scans succeeded, "
          f"stable node set {results[0]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
