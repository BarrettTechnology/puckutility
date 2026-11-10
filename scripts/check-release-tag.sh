#!/usr/bin/env bash
# Check that a release tag may be pushed: it names the version the app reports
# and the window title does not say DEV. The CI release job and the pre-push
# hook (.githooks/pre-push) both run this, so a tag that CI would reject is
# stopped before it leaves your machine.
#
#   scripts/check-release-tag.sh TAG [COMMIT]     COMMIT defaults to HEAD
#
# The files are read from COMMIT (git show), not from the working tree; with
# no COMMIT and no git (an older CI image), from the checked-out tree.
set -euo pipefail

APP=puckutility
tag="${1:?usage: $0 TAG [COMMIT]}"
rev="${2:-HEAD}^{commit}"
from_tree=false
if [ $# -lt 2 ] && ! command -v git >/dev/null; then from_tree=true; fi
show() { if $from_tree; then cat "$1"; else git show "$rev:$1"; fi; }

version="$(show "$APP/version.py" | grep -m1 '^__version__' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
[ -n "$version" ] || { echo "ERROR: no __version__ in $APP/version.py at ${rev%^*}" >&2; exit 1; }

case "$tag" in
    "release-v$version"|"release-v$version"-*) ;;
    *) echo "ERROR: tag $tag does not match __version__ $version (expected release-v$version[-suffix])" >&2
       exit 1 ;;
esac
if show "$APP/gui/app.py" | grep -m1 SetTitle | grep -qiE '\bDEV\b'; then
    echo "ERROR: tag $tag: the window title ($APP/gui/app.py SetTitle) still says DEV" >&2
    exit 1
fi
echo "release tag $tag: version $version ok"
