#!/bin/bash
# Assemble the A/B test package folder (plan: p4gui notes/ab-test-plan.md §1, §10).
#
#   abtest/make_package.sh [--force] [--firmware DIR] [OUTDIR]
#
# OUTDIR defaults to ~/code/p4gui/ab-testing-YYYYMMDD and is not overwritten
# unless --force. The folder is not zipped.
#
# Firmware: by default the plain STM32 .bin images built at the tags
# (./build.sh stm32 app release in a checkout at v4.3.5 and at v5.0.5),
# found in $STM32 or one of its worktrees. --firmware DIR copies the
# puck4.app.stm32.* images (.ebin or .bin) from DIR instead.
#
# Inputs from this repo: abtest/{ab-testing,install,can-adapters}.md,
# abtest/README-package.md, abtest/{ab_testing,csvdiff}.py, sandbox/testp4.py,
# abtest/csv, abtest/eds, abtest/docs, build/ installers. Manuals come from
# $STM32 git: $MANUAL_435 (branch) and the v5.0.5 tag, refreshed by
# abtest/odt2pdf.py.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
STM32=${STM32:-$HOME/code/stm32}
P4GUI=${P4GUI:-$HOME/code/p4gui}
PYTHON=${PYTHON:-$P4GUI/bin/python3}
UNO_PYTHON=${UNO_PYTHON:-/usr/bin/python3}
MANUAL_435=${MANUAL_435:-feature/manual-4.3.5}
TAGS=(v4.3.5 v5.0.5)

force=0
fwdir=
out=
while [ $# -gt 0 ]; do
    case "$1" in
        --force) force=1 ;;
        --firmware) fwdir=$2; shift ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) out=$1 ;;
    esac
    shift
done
out=${out:-$P4GUI/ab-testing-$(date +%Y%m%d)}
out=$(realpath -m "$out")

die() { echo "make_package: $*" >&2; exit 1; }

if [ -e "$out" ] && [ $force = 0 ]; then
    die "$out exists (use --force to replace it)"
fi

ver=$("$PYTHON" -c "import runpy; print(runpy.run_path('$REPO/puckutility/version.py')['__version__'])")
run=$REPO/build/PuckUtilityApp-lin-v$ver-installer.run
exe=$REPO/build/win/PuckUtilityApp-win-v$ver-setup.exe
[ -f "$run" ] || die "no Linux installer $run (build it on Ubuntu 20.04)"
[ -f "$exe" ] || die "no Windows installer $exe (build-win32-wine.sh)"

# Firmware images, one per tag.
fw=()
if [ -n "$fwdir" ]; then
    for t in "${TAGS[@]}"; do
        f=$(ls "$fwdir"/puck4.app.stm32.*"$t"*.ebin "$fwdir"/puck4.app.stm32.*"$t"*.bin 2>/dev/null | head -1 || true)
        [ -n "$f" ] || die "no puck4.app.stm32 image for $t in $fwdir"
        fw+=("$f")
    done
else
    for t in "${TAGS[@]}"; do
        name=puck4.app.stm32.release.$(git -C "$STM32" describe --tags --long "$t").bin
        f=$(ls "$STM32/$name" "$STM32"/worktrees/*/"$name" 2>/dev/null | head -1 || true)
        [ -n "$f" ] || die "no $name in $STM32 or its worktrees: ./build.sh stm32 app release at $t"
        fw+=("$f")
    done
fi

tmp=$out.tmp.$$
rm -rf "$tmp"
mkdir -p "$tmp"/{csv,eds,docs,firmware,wheels,installers/linux,installers/windows}
trap 'rm -rf "$tmp"' EXIT

echo "== scripts and guides"
cp "$HERE/README-package.md" "$tmp/README.md"
cp "$HERE/ab-testing.md" "$HERE/install.md" "$HERE/can-adapters.md" "$tmp/"
cp "$HERE/ab_testing.py" "$HERE/csvdiff.py" "$REPO/sandbox/testp4.py" "$tmp/"

echo "== csv, eds"
cp "$HERE"/csv/*.csv "$tmp/csv/"
cp "$HERE"/eds/puck4-4.3.5.eds "$HERE"/eds/puck4-5.0.5.eds "$tmp/eds/"
for c in "$tmp"/csv/*.csv; do
    case "$(basename "$c")" in
        *fw4.3.5*) eds=$tmp/eds/puck4-4.3.5.eds ;;
        *fw5.0.5*) eds=$tmp/eds/puck4-5.0.5.eds ;;
        *) die "$(basename "$c"): no fw4.3.5/fw5.0.5 in the name" ;;
    esac
    printf '%s: ' "$(basename "$c")"
    PYTHONPATH=$REPO "$PYTHON" "$HERE/csvaudit.py" "$c" "$eds" | tail -1
done

echo "== firmware"
cp "${fw[@]}" "$tmp/firmware/"
ls "$tmp/firmware"

echo "== manuals"
git -C "$STM32" show "$MANUAL_435:docs/manual/P4_CANopen_Manual.odt" > "$tmp/docs/P4_CANopen_Manual-4.3.5.odt"
git -C "$STM32" show "v5.0.5:docs/manual/P4_CANopen_Manual.odt" > "$tmp/docs/P4_CANopen_Manual-5.0.5.odt"
lo=$(mktemp -d)
"$UNO_PYTHON" "$HERE/odt2pdf.py" "$lo" "$tmp"/docs/P4_CANopen_Manual-*.odt
rm -rf "$lo"
cp "$HERE"/docs/*.html "$tmp/docs/"

echo "== wheels"
"$PYTHON" -m pip wheel -q --no-deps --no-build-isolation -w "$tmp/wheels" "$REPO/p4core" "$REPO"
ls "$tmp/wheels"

echo "== installers"
cp "$run" "$tmp/installers/linux/"
cp "$exe" "$tmp/installers/windows/"

(cd "$tmp" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort | xargs -d '\n' sha256sum > SHA256SUMS)

trap - EXIT
rm -rf "$out"
mv "$tmp" "$out"
echo "== $out"
du -sh "$out"
