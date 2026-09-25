#!/bin/bash
# Put the Pi's boot display config back to the original (touch-working) state:
# restores config.txt and cmdline.txt from the .bak-pendulum-display backups,
# checks both look sane, and reboots ONLY if they do.
#
#   ./restore-display.sh
set -u
FW=/boot/firmware
fail() { echo; echo "XX  $*"; echo "    NOT rebooting. Tell Claude what this printed."; exit 1; }

for f in config.txt cmdline.txt; do
    [ -e "$FW/$f.bak-pendulum-display" ] || fail "missing backup $FW/$f.bak-pendulum-display"
done

sudo cp "$FW/config.txt.bak-pendulum-display"  "$FW/config.txt"  || fail "couldn't restore config.txt"
sudo cp "$FW/cmdline.txt.bak-pendulum-display" "$FW/cmdline.txt" || fail "couldn't restore cmdline.txt"

echo "== config.txt (first lines)"; head -5 "$FW/config.txt"
echo "== cmdline.txt"; cat "$FW/cmdline.txt"

grep -q '^arm_64bit=1'            "$FW/config.txt"  || fail "config.txt doesn't look like the Pi boot config"
grep -q '^kernel=vmlinuz'         "$FW/config.txt"  || fail "config.txt has no kernel= line"
grep -q '^display_auto_detect=1'  "$FW/config.txt"  || fail "config.txt isn't the auto-detect original"
grep -q 'root=LABEL=writable'     "$FW/cmdline.txt" || fail "cmdline.txt has no root= entry"
grep -q 'video=HDMI'              "$FW/cmdline.txt" && fail "cmdline.txt still has video=HDMI"
[ "$(wc -l <"$FW/cmdline.txt")" -le 1 ] || fail "cmdline.txt has more than one line"

echo
echo "OK  both files restored and look right. Rebooting in 5 s (Ctrl+C to cancel)..."
sleep 5
sudo reboot
