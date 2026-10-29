#!/usr/bin/env bash
# DEB half of the cross-Ubuntu test. Run as root INSIDE a clean ubuntu:XX.04
# container (scripts/test-cross-ubuntu.sh via docker, or a GitLab CI job whose
# image is that Ubuntu). Runs ONLY the installer, so apt resolves the packages'
# declared Depends (the GTK/X11 runtime stack, can-utils, dfu-util): this
# VALIDATES that the debs are self-sufficient on a minimal system -- a runtime
# lib missing from Depends crashes the launch below and names it. Then checks
# the CAN adapter options (select, deselect, re-add), launches the installed
# binary under a virtual display, and uninstalls.
#
# Usage: scripts/xtest/deb.sh build/PuckUtilityApp-lin-vX.Y.Z-installer.run
set -euo pipefail
R="$(realpath "$1")"
export DEBIAN_FRONTEND=noninteractive
has() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed'; }
run_installer() {  # $@ = installer options; prints its output only on failure
    bash "$R" --yes "$@" >/tmp/inst.out 2>&1 || { cat /tmp/inst.out; exit 1; }
}

echo '  installer: both adapters'
run_installer
for f in /usr/lib/udev/rules.d/60-can.rules /usr/lib/udev/rules.d/61-can-up.rules \
         /usr/lib/udev/rules.d/90-canable.rules /usr/lib/systemd/system/can-up@.service \
         /etc/NetworkManager/conf.d/99-puck-can.conf /usr/bin/dfu-util /usr/bin/candump; do
    [ -e "$f" ] || { echo "  missing $f"; exit 1; }
done
echo '  installer: deselect Peak'
run_installer --no-peak
! has puck-can-peak || { echo '  puck-can-peak still installed'; exit 1; }
has puck-can-canable || { echo '  puck-can-canable was removed'; exit 1; }
echo '  installer: re-add Peak'
run_installer
has puck-can-peak || { echo '  puck-can-peak not reinstalled'; exit 1; }

apt-get install -y -qq xvfb >/dev/null
BIN=$(ls /usr/share/*/*App 2>/dev/null | head -1)
[ -n "$BIN" ] || { echo 'installed binary not found'; exit 1; }
echo "  launching under xvfb: $BIN"
# The app has no --help short-circuit; it starts the GUI. Time-box it under a
# virtual display: a clean exit (rc 0) or still-running-at-timeout (rc 124) both
# mean every bundled+system lib loaded and wx.App() came up. An ImportError /
# symbol-version clash crashes in <2s with some other non-zero rc.
set +e
timeout 20 xvfb-run -a "$BIN" >/tmp/run.out 2>&1
rc=$?
set -e
if [ "$rc" = 0 ] || [ "$rc" = 124 ]; then
    echo "  launch OK (rc=$rc: libs loaded, wx.App came up)"
else
    echo "  launch FAILED (rc=$rc) -- binary output:"; cat /tmp/run.out; exit 1
fi

echo '  installer: uninstall'
run_installer --uninstall
for p in puckutilityapp puck-can-common puck-can-peak puck-can-canable; do
    ! has "$p" || { echo "  $p still installed"; exit 1; }
done
[ ! -e /etc/NetworkManager/conf.d/99-puck-can.conf ] || { echo '  NM conf left behind'; exit 1; }
echo '  uninstall OK'
