#!/usr/bin/env python3
"""
test_fd.py  –  Demo script for the CandlelightBus driver.

Demonstrates:
  - Classic CAN  send/receive  @ 1 Mbps
  - CAN FD       send/receive  @ 1 Mbps nominal / 5 Mbps data

Requirements (Windows, Python 3.12):
  pip install pyusb python-can
  libusb-1.0.dll v1.0.29  in the script directory or on PATH
  CANable 2.5 with Candlelight firmware and WinUSB driver installed

Demo mode:
  Set LOOPBACK = True to receive your own transmissions on the same adapter
  (useful when no second node is on the bus).
"""

import time

import can

from candlelight_bus import CandlelightBus

# ── Demo knob ────────────────────────────────────────────────────────────────
LOOPBACK = False   # True → adapter echoes TX back as RX (no second node needed)


# ─────────────────────────────────────────────────────────────────────────────
def demo_classic_can(loopback: bool = False):
    print("\n── Classic CAN @ 1 Mbps " + "─" * 48)
    with CandlelightBus(channel=0, bitrate=1_000_000, loopback=loopback,
                        sample_point=0.75) as bus:
        payload = bytes([0x40, 0x41, 0x60, 0x00])
        tx = can.Message(arbitration_id=0x67F, data=payload, is_extended_id=False,
                         timestamp=time.time())
        print(f"TX: {tx}")
        bus.send(tx)

        rx = bus.recv(timeout=2.0)
        print(f"RX: {rx}" if rx else "RX: (timeout – no frame received)")


def demo_can_fd(loopback: bool = False):
    print("\n── CAN FD @ 1 Mbps nominal / 5 Mbps data " + "─" * 31)
    with CandlelightBus(channel=0, bitrate=1_000_000, fd=True,
                        data_bitrate=5_000_000, loopback=loopback,
                        sample_point=0.75, data_sample_point=0.75) as bus:
        payload = bytes([0x40, 0x41, 0x60, 0x00])
        tx = can.Message(arbitration_id=0x67F, data=payload,
                         is_extended_id=False, is_fd=True, bitrate_switch=True,
                         timestamp=time.time())
        print(f"TX: {tx}")
        bus.send(tx)

        rx = bus.recv(timeout=2.0)
        print(f"RX: {rx}" if rx else "RX: (timeout – no frame received)")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    demo_classic_can(loopback=LOOPBACK)

    time.sleep(0.1)

    demo_can_fd(loopback=LOOPBACK)
