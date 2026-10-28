#!/bin/sh
# Puck Utility @VERSION@ installer for Ubuntu 20.04 - 26.04 (x86_64).
#
#   chmod +x @NAME@ && ./@NAME@             install (asks which CAN adapters)
#   ./@NAME@ --help                         installer options
#   ./@NAME@ --extract DIR                  unpack the packages only
#
# The installer and its .deb packages are a gzip'd tar after the marker line.
set -e
SKIP=@SKIP@
SHA256=@SHA256@

if command -v sha256sum >/dev/null 2>&1; then
    sum=$(tail -n +"$SKIP" "$0" | sha256sum | cut -d' ' -f1)
    if [ "$sum" != "$SHA256" ]; then
        echo "ERROR: $0 is damaged (checksum mismatch); download it again." >&2
        exit 1
    fi
fi

if [ "$1" = "--extract" ]; then
    dest="${2:-puckutility-installer}"
    mkdir -p "$dest"
    tail -n +"$SKIP" "$0" | tar -xzf - -C "$dest"
    echo "Unpacked to $dest  (run $dest/install.sh, or apt install $dest/debs/*.deb)"
    exit 0
fi

tmp=$(mktemp -d /tmp/puckutility-installer.XXXXXX)
trap 'rm -rf "$tmp"' EXIT INT TERM
tail -n +"$SKIP" "$0" | tar -xzf - -C "$tmp"
# apt reads local .debs as its unprivileged _apt user: make them readable.
chmod -R a+rX "$tmp"
rc=0
bash "$tmp/install.sh" "$@" || rc=$?
exit $rc
__PAYLOAD_BELOW__
