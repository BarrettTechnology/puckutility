#!/bin/bash
# Reset / re-configure a SocketCAN interface (default can0).
# Tries CAN-FD (1 Mbit nominal / 5 Mbit data phase, 68.7 % sample point, the
# bench SJW), then CAN-FD on the kernel's default timing, then classic 1 Mbit
# for adapters that do not support FD (same order as can-up@.service). restart-ms (auto-recover from
# bus-off) is applied separately as best-effort -- the gs_usb / CANable adapter
# rejects it ("Device doesn't support restart from Bus Off").
IFACE="${1:-can0}"
sudo ip link set "$IFACE" down 2>/dev/null
sudo ip link set "$IFACE" type can bitrate 1000000 sample-point 0.687 sjw 50 dbitrate 5000000 dsample-point 0.687 dsjw 10 fd on \
    || sudo ip link set "$IFACE" type can bitrate 1000000 dbitrate 5000000 fd on \
    || sudo ip link set "$IFACE" type can bitrate 1000000
sudo ip link set "$IFACE" type can restart-ms 100 2>/dev/null || true
sudo ip link set "$IFACE" txqueuelen 1000
sudo ip link set "$IFACE" up
echo "CAN interface reset: $IFACE"
