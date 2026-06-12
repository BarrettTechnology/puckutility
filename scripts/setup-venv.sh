#!/usr/bin/env bash
# Bootstrap a reproducible Python 3.13 dev environment that does NOT depend on
# the system Python or whatever ships in apt.  uv fetches a *prebuilt standalone*
# CPython 3.13 (from the python-build-standalone project), so this produces an
# identical interpreter on Ubuntu 20.04 -> 26.04+ regardless of the distro's apt.
# (3.13, not 3.14: it is the newest Python with prebuilt wxPython + scientific
# wheels, so the whole stack installs without compiling anything.)
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Runtime libs for the prebuilt wxPython wheel -----------------------------
# wxPython is installed from a prebuilt cp313 wheel (see requirements.txt), so
# nothing is compiled.  The wheel only needs these shared libraries present at
# runtime; they are not build/-dev packages.  (libgtk-3-0 is usually already
# installed on a desktop; libsdl2 often is not.)
# Use sudo only when not already root and sudo exists (CI/containers run as root
# without sudo installed).
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO=sudo; fi
$SUDO apt update
# The prebuilt wxPython wheel links a chain of shared libs directly, so apt
# can't pull them as wheel dependencies. A desktop already has them transitively
# but a minimal install / container does not, so install them explicitly. This
# is the set the wx _core/_adv extensions need; surfaced by failing `import wx`
# on clean 20.04-26.04 via scripts/test-cross-ubuntu.sh. libtiff's soname tracks
# the wheel base (see setup-pip.sh): the 20.04/22.04 wheels link libtiff.so.5,
# the 24.04 wheel (used for 24.04 and newer) links libtiff.so.6 — and the two
# packages are mutually exclusive per release, so pick the one matching the host.
LIBTIFF=libtiff6
if [ -r /etc/os-release ]; then
    case "$(. /etc/os-release; echo "${VERSION_ID:-}")" in
        20.*|21.*|22.*|23.*) LIBTIFF=libtiff5 ;;
    esac
fi
$SUDO apt install -y libgtk-3-0 libsdl2-2.0-0 \
    libnotify4 libsm6 libxxf86vm1 libpcre2-32-0 \
    libsecret-1-0 libjpeg-turbo8 "$LIBTIFF"

# --- uv: portable Python + venv manager (installs to ~/.local/bin, no sudo) ---
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Standalone CPython 3.13 + venv at repo-root .venv ------------------------
uv python install 3.13
# Create the venv only if absent, so re-runs don't wipe an existing one.
# Pass --recreate-venv to force a clean rebuild.
if [ "$1" = "--recreate-venv" ] || [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    # --seed puts pip in the venv so `pip install X` works after activation.
    # --prompt sets the activated-shell label to the repo name (not ".venv").
    uv venv --seed --clear --prompt "$(basename "$REPO_ROOT")" --python 3.13 "$REPO_ROOT/.venv"
fi

# Preserve the historical `source scripts/activate` entry point.
ln -sf ../.venv/bin/activate "$SCRIPT_DIR/activate"

# --- Install all Python dependencies -----------------------------------------
"$SCRIPT_DIR/setup-pip.sh"

echo
echo "Done.  Activate with:  source scripts/activate"
echo "                 (or:  source .venv/bin/activate)"
