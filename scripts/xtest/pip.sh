#!/usr/bin/env bash
# PIP half of the cross-Ubuntu test. Run as root INSIDE a clean ubuntu:XX.04
# container (scripts/test-cross-ubuntu.sh via docker): install the app from a
# copy of the checkout both ways the README documents, NOT editable, so the
# wheel (and the package data it carries) is what runs:
#
#   CLI : pip install ./p4core [./p4sysid] .  -- wxPython must not come in;
#         --version, --help and every non-GUI module work without it, and the
#         GUI entry point says how to install the GUI instead of crashing.
#   GUI : the same with ".[gui]" and the prebuilt wxPython wheel for this
#         Ubuntu (--only-binary: a source build of wxWidgets would take 20+
#         minutes and needs the GTK headers); <app>-gui comes up under a
#         virtual display.
#
# Not as root (outside a container) the apt step is skipped: the host needs
# curl, xvfb and the GTK runtime already.
#
# Usage: scripts/xtest/pip.sh [REPO_DIR]   (default: this checkout)
set -euo pipefail
SRC="$(cd "${1:-$(dirname "$0")/../..}" && pwd)"
APP="$(sed -n 's/^name = "\(.*\)"$/\1/p' "$SRC/pyproject.toml" | head -1)"
export DEBIAN_FRONTEND=noninteractive
fail() { echo "  FAIL: $*"; exit 1; }

VER=$(. /etc/os-release; echo "${UBUNTU_VERSION_ID:-${VERSION_ID:-}}")
if [ "$(id -u)" -eq 0 ]; then
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates xvfb >/dev/null
    # The GUI's runtime libraries, as scripts/setup-venv.sh installs them
    # (24.04+ renamed some to <name>t64).
    LIBS=()
    for p in libgtk-3-0 libsdl2-2.0-0 libnotify4 libsm6 libxxf86vm1 \
             libpcre2-32-0 libsecret-1-0 libjpeg-turbo8 libtiff5 libtiff6; do
        for c in "$p" "${p}t64"; do
            if apt-cache policy "$c" 2>/dev/null | grep -q 'Candidate: [^(]'; then
                LIBS+=("$c"); break
            fi
        done
    done
    apt-get install -y -qq "${LIBS[@]}" >/dev/null
fi
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    export PATH="$HOME/.local/bin:$PATH"
fi

# The wxPython wheel built for this Ubuntu (scripts/setup-pip.sh's mapping).
case "$VER" in
    20.*|21.*) WX_BASE=ubuntu-20.04 ;;
    22.*|23.*) WX_BASE=ubuntu-22.04 ;;
    *)         WX_BASE=ubuntu-24.04 ;;
esac
WX_LINKS="https://extras.wxpython.org/wxPython4/extras/linux/gtk3/${WX_BASE}/"

# Work on a copy: the checkout may be read-only, and a build must not leave
# egg-info or a venv in it.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/src"
tar -C "$SRC" --exclude=./bin --exclude=./lib --exclude=./lib64 \
    --exclude=./.venv --exclude=./build --exclude=./logs --exclude=./sandbox \
    --exclude=./data_dump --exclude=.git --exclude=__pycache__ -cf - . \
    | tar -C "$WORK/src" -xf -
cd "$WORK/src"
DEPS=(./p4core)
[ -f p4sysid/pyproject.toml ] && DEPS+=(./p4sysid)

echo "  CLI: pip install ${DEPS[*]} ."
uv venv -q --python 3.12 "$WORK/cli"
uv pip install -q --python "$WORK/cli" "${DEPS[@]}" .
cd "$WORK"
"$WORK/cli/bin/python" -c 'import wx' 2>/dev/null \
    && fail 'wxPython came in with the command-line install'
out=$("$WORK/cli/bin/$APP" --version)
echo "    $out"
[[ "$out" == "$APP v"* ]] || fail "--version printed '$out'"
"$WORK/cli/bin/$APP" --help >/dev/null || fail '--help'
"$WORK/cli/bin/python" - "$APP" <<'PY' || fail 'a module needs wxPython'
import importlib, pkgutil, sys
app = sys.argv[1]
pkg = importlib.import_module(app)
bad = []
for m in pkgutil.walk_packages(pkg.__path__, app + '.'):
    if m.name.startswith(app + '.gui') or m.name.endswith('__main__'):
        continue
    try:
        importlib.import_module(m.name)
    except Exception as exc:
        bad.append('{}: {}'.format(m.name, exc))
print('    {} non-GUI modules import'.format(
    sum(1 for k in sys.modules if k.startswith(app + '.'))))
for b in bad:
    print('    import failed:', b)
sys.exit(1 if bad or 'wx' in sys.modules else 0)
PY
set +e
env -u DISPLAY "$WORK/cli/bin/$APP-gui" >"$WORK/nogui.out" 2>&1
rc=$?
set -e
[ "$rc" = 2 ] && grep -q 'needs wxPython' "$WORK/nogui.out" \
    || { cat "$WORK/nogui.out"; fail "$APP-gui without wxPython (rc=$rc)"; }
echo "    $APP-gui without wxPython: says how to install the GUI"

echo "  GUI: pip install ${DEPS[*]} \".[gui]\" (wxPython from ${WX_BASE})"
cd "$WORK/src"
uv venv -q --python 3.12 "$WORK/gui"
uv pip install -q --python "$WORK/gui" --only-binary wxPython \
    --find-links "$WX_LINKS" "${DEPS[@]}" ".[gui]"
cd "$WORK"
echo "    $("$WORK/gui/bin/python" -c 'import wx; print("wx", wx.__version__)')"
# As in deb.sh: a clean exit (0) or still running at the timeout (124) means
# every library loaded and the window came up.
set +e
timeout 20 xvfb-run -a "$WORK/gui/bin/$APP-gui" >"$WORK/gui.out" 2>&1
rc=$?
set -e
if [ "$rc" = 0 ] || [ "$rc" = 124 ]; then
    echo "    $APP-gui launch OK (rc=$rc)"
else
    cat "$WORK/gui.out"; fail "$APP-gui launch (rc=$rc)"
fi
