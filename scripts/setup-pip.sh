#!/usr/bin/env bash
# (Re)install the Python dependencies into the project venv (.venv) using uv.
# Run scripts/setup-venv.sh first to create the venv; this script just syncs
# requirements.txt into it (handy after editing requirements.txt).
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Run scripts/setup-venv.sh first." >&2
    exit 1
fi

# wxPython has no PyPI Linux wheel; its prebuilt wheels live on the extras index,
# built per-Ubuntu. Each wheel links the shared-lib sonames of its base Ubuntu
# (e.g. the 20.04 wheel needs libtiff.so.5 + webkit-4.0, which newer Ubuntu has
# dropped). So pick the base that MATCHES this host. Wheels exist only for
# 20.04 / 22.04 / 24.04; map anything newer (e.g. 26.04) to the newest, 24.04.
WX_BASE=ubuntu-24.04
if [ -r /etc/os-release ]; then
    HOST_VER=$(. /etc/os-release; echo "${VERSION_ID:-}")
    case "$HOST_VER" in
        20.*|21.*) WX_BASE=ubuntu-20.04 ;;
        22.*|23.*) WX_BASE=ubuntu-22.04 ;;
        *)         WX_BASE=ubuntu-24.04 ;;   # 24.04 and newer (incl. 26.04)
    esac
fi
WX_FIND_LINKS="https://extras.wxpython.org/wxPython4/extras/linux/gtk3/${WX_BASE}/"
echo "Using wxPython prebuilt wheels for: ${WX_BASE}"

uv pip install --python "$REPO_ROOT/.venv" \
    --find-links "$WX_FIND_LINKS" \
    -r "$REPO_ROOT/requirements.txt"

# --- Dev-only tools (optional) ------------------------------------------------
# wxGlade (and any future dev tooling) for editing the GUI: it regenerates
# *_gui.py from configure-*.wxg. Installed into the SOURCE venv only — never
# bundled into the .deb (the app never imports it, and the Docker build installs
# requirements.txt, not this file). wxGlade is a git install, so it needs `git`
# on PATH. Skip entirely with NO_DEV_TOOLS=1 (e.g. in a minimal/CI environment).
if [ "${NO_DEV_TOOLS:-}" != "1" ] && [ -f "$REPO_ROOT/requirements-dev.txt" ]; then
    echo "Installing dev tools (requirements-dev.txt)..."
    uv pip install --python "$REPO_ROOT/.venv" \
        --find-links "$WX_FIND_LINKS" \
        -r "$REPO_ROOT/requirements-dev.txt"
fi
