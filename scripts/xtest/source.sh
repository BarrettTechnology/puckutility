#!/usr/bin/env bash
# SOURCE half of the cross-Ubuntu test. Run as root INSIDE a clean ubuntu:XX.04
# container (scripts/test-cross-ubuntu.sh via docker, or a GitLab CI job whose
# image is that Ubuntu): set up the dev venv with scripts/setup-venv.sh and check
# `import wx` + wx.App() under a virtual display, using the container's OWN
# system GLib/GTK.
#
# Usage: scripts/xtest/source.sh [REPO_DIR]   (default: this checkout)
set -euo pipefail
SRC="$(cd "${1:-$(dirname "$0")/../..}" && pwd)"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq curl ca-certificates xvfb >/dev/null
# Work on a copy so the venv never lands in the (possibly read-only) checkout.
WORK="$(mktemp -d)"
mkdir -p "$WORK/scripts"
cp "$SRC/requirements.txt" "$WORK/"
cp -a "$SRC/scripts/." "$WORK/scripts/"
cd "$WORK"
bash scripts/setup-venv.sh
xvfb-run -a .venv/bin/python - <<'PY'
import importlib, wx
app = wx.App()
print("  wx", wx.__version__, "App() OK")
# Python 3.13 + wxPython 4.2.2 broke these iterators; the apps walk them.
f = wx.Frame(None)
s = wx.BoxSizer()
s.Add(wx.Button(f))
f.SetSizer(s)
assert len([w for w in f.GetChildren()]) == 1
assert len([i for i in s.GetChildren()]) == 1
print("  GetChildren() iteration OK")
for m in "canopen can msgpack numpy pandas matplotlib PIL fontTools PyInstaller".split():
    try:
        importlib.import_module(m); print("  import", m, "OK")
    except ModuleNotFoundError:
        pass
PY
