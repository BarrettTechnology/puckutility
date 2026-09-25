#!/bin/bash
# Blind-safe recovery: undo the Barrett boot splash and reboot, with Caps Lock
# light signals (for a black screen). Run from a text console (Ctrl+Alt+F4):
#     cd puckutility
#     git pull
#     sandbox/fix-now.sh        (then your password)
#   2 blinks  = started
#   5 blinks  = done, rebooting now
cd "$(dirname "$0")/.." || exit 1
blink() { for i in $(seq "$1"); do setleds +caps; sleep .35; setleds -caps; sleep .35; done; }
sudo -v || exit 1
blink 2
# If an earlier undo/rebuild is still running, let it finish first.
while pgrep -f 'update-initramfs|mkinitramfs|install-splash' >/dev/null; do sleep 2; done
cur=$(update-alternatives --query default.plymouth 2>/dev/null | awk '/^Value:/{print $2}')
case "$cur" in
    *barrett-pendulum*) sandbox/install-splash.sh --undo > ~/splash-undo.log 2>&1 ;;
esac
blink 5
sudo reboot
