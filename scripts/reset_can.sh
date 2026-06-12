#!/bin/bash
# Reset / re-configure a SocketCAN interface (default can0).
# Tries CAN-FD (1 Mbit nominal / 5 Mbit data phase); falls back to classic
# 1 Mbit for adapters that do not support FD. restart-ms (auto-recover from
# bus-off) is applied separately as best-effort -- the gs_usb / CANable adapter
# rejects it ("Device doesn't support restart from Bus Off").
IFACE="${1:-can0}"
sudo ip link set "$IFACE" down 2>/dev/null
sudo ip link set "$IFACE" type can bitrate 1000000 fd on dbitrate 5000000 \
    || sudo ip link set "$IFACE" type can bitrate 1000000
sudo ip link set "$IFACE" type can restart-ms 100 2>/dev/null || true
sudo ip link set "$IFACE" txqueuelen 1000
sudo ip link set "$IFACE" up
echo "CAN interface reset: $IFACE"
