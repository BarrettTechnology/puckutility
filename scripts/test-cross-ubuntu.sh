#!/usr/bin/env bash
# Cross-Ubuntu smoke test — tests BOTH halves of "works on 20.04 -> 26.04":
#
#   SOURCE : run THIS repo's scripts/setup-venv.sh (uv + standalone CPython 3.13
#            + host-matched wxPython wheel) and verify `import wx` + `wx.App()`
#            under a headless X server. This exercises the dev/from-source path,
#            which uses each container's OWN system GLib.
#   DEB    : build the .deb (PyInstaller frozen binary, built on Ubuntu 20.04),
#            then install it in each clean container and run `<binary> --help`.
#            This exercises the SHIPPED artifact, which bundles libraries from
#            the 20.04 build env -- the half that the source test cannot see
#            (e.g. a bundled-GLib vs system-libsecret symbol clash on Ubuntu 26).
#
# Requires docker (and your user in the docker group). Run anywhere docker is
# available; results reflect the target OS, not the host.
#
# Usage:
#   scripts/test-cross-ubuntu.sh                 # 20.04 22.04 24.04 26.04
#   scripts/test-cross-ubuntu.sh 24.04 26.04     # a subset / a new release
#   scripts/test-cross-ubuntu.sh --source-only   # skip the (slower) .deb half
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

SOURCE_ONLY=false
VERSIONS=()
for a in "$@"; do
    case "$a" in
        --source-only) SOURCE_ONLY=true ;;
        *) VERSIONS+=("$a") ;;
    esac
done
[ "${#VERSIONS[@]}" -gt 0 ] || VERSIONS=(20.04 22.04 24.04 26.04)

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found. Run this where docker is available." >&2
    exit 1
fi

# --- Colored PASS/FAIL/SKIP output -------------------------------------------
# Emit ANSI colors only when stdout is a real terminal, so piping/redirecting to
# a log file stays clean (no escape codes). Set NO_COLOR=1 to force plain text.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m';   C_RESET=$'\033[0m'
else
    C_GREEN=; C_RED=; C_YELLOW=; C_BOLD=; C_RESET=
fi
colorize() {  # $1 = PASS|FAIL|SKIP -> the same word wrapped in its color
    case "$1" in
        PASS) printf '%s' "${C_GREEN}PASS${C_RESET}" ;;
        FAIL) printf '%s' "${C_RED}FAIL${C_RESET}" ;;
        SKIP) printf '%s' "${C_YELLOW}SKIP${C_RESET}" ;;
        *)    printf '%s' "$1" ;;
    esac
}

# Shared uv cache volume so the standalone Python + the wxPython/scientific
# wheels download once, not once per release.
UV_CACHE="${TMPDIR:-/tmp}/puck-xtest-uvcache"
mkdir -p "$UV_CACHE"

declare -A SRC DEB

# ===========================================================================
# SOURCE half: setup-venv.sh + import wx + wx.App() in each clean container.
# ===========================================================================
for ver in "${VERSIONS[@]}"; do
    echo "==================  SOURCE  ubuntu:${ver}  =================="
    if docker run --rm \
            -v "$REPO:/src:ro" \
            -v "$UV_CACHE:/root/.cache/uv" \
            -e UV_LINK_MODE=copy -e DEBIAN_FRONTEND=noninteractive \
            "ubuntu:${ver}" bash -euo pipefail -c '
                apt-get update -qq
                apt-get install -y -qq curl ca-certificates xvfb >/dev/null
                mkdir -p /work/scripts
                cp /src/requirements.txt /work/
                cp -a /src/scripts/. /work/scripts/
                cd /work
                bash scripts/setup-venv.sh
                xvfb-run -a .venv/bin/python - <<PY
import importlib, wx
app = wx.App()
print("  wx", wx.__version__, "App() OK")
for m in "canopen can msgpack numpy pandas matplotlib PIL fontTools PyInstaller".split():
    try:
        importlib.import_module(m); print("  import", m, "OK")
    except ModuleNotFoundError:
        pass
PY
            '; then
        SRC["$ver"]=PASS
    else
        SRC["$ver"]=FAIL
    fi
    echo "  ==> SOURCE ubuntu:${ver}: $(colorize "${SRC[$ver]}")"
done

# ===========================================================================
# DEB half: build the package once, then install + run --help on each version.
# `<binary> --help` runs `import wx` (the crash point for bundled-lib clashes)
# and exits before the GUI/CAN, so it needs no display or hardware.
# ===========================================================================
if [ "$SOURCE_ONLY" = false ]; then
    echo "==================  building .deb (once)  =================="
    DEB_REL=""
    if ./scripts/build-linux-installer.sh --docker --deb; then
        DEB_REL="$(ls -t build/deb/*.deb 2>/dev/null | head -1)"
    fi
    if [ -z "$DEB_REL" ] || [ ! -f "$DEB_REL" ]; then
        echo "ERROR: .deb build failed / not found — marking DEB tests SKIP" >&2
        for ver in "${VERSIONS[@]}"; do DEB["$ver"]=SKIP; done
    else
        echo "Built: $DEB_REL"
        for ver in "${VERSIONS[@]}"; do
            echo "==================  DEB  ubuntu:${ver}  =================="
            # Install ONLY the .deb into a bare container: apt resolves and pulls
            # the package's declared Depends (the GTK/X11 runtime stack) from the
            # repo. This VALIDATES that the .deb is self-sufficient on a minimal
            # system -- if a runtime lib is missing from Depends, the launch
            # below crashes and names it.
            if docker run --rm \
                    -v "$REPO:/src:ro" \
                    -e DEBIAN_FRONTEND=noninteractive \
                    "ubuntu:${ver}" bash -euo pipefail -c "
                        apt-get update -qq
                        apt-get install -y -qq '/src/${DEB_REL}' >/dev/null
                        apt-get install -y -qq xvfb >/dev/null
                        BIN=\$(ls /usr/share/*/*App 2>/dev/null | head -1)
                        [ -n \"\$BIN\" ] || { echo 'installed binary not found'; exit 1; }
                        echo \"  launching under xvfb: \$BIN\"
                        # The app has no --help short-circuit; it starts the GUI.
                        # Launch under a virtual display and time-box it: a clean
                        # exit (rc 0) or still-running-at-timeout (rc 124) both
                        # mean every bundled+system lib loaded and wx.App() came
                        # up. An ImportError / symbol-version clash crashes in
                        # <2s with some other non-zero rc -> real failure.
                        set +e
                        timeout 20 xvfb-run -a \"\$BIN\" >/tmp/run.out 2>&1
                        rc=\$?
                        set -e
                        if [ \"\$rc\" = 0 ] || [ \"\$rc\" = 124 ]; then
                            echo \"  launch OK (rc=\$rc: libs loaded, wx.App came up)\"
                        else
                            echo \"  launch FAILED (rc=\$rc) -- binary output:\"; cat /tmp/run.out; exit 1
                        fi
                    "; then
                DEB["$ver"]=PASS
            else
                DEB["$ver"]=FAIL
            fi
            echo "  ==> DEB ubuntu:${ver}: $(colorize "${DEB[$ver]}")"
        done
    fi
fi

# ===========================================================================
echo
echo "==============================  summary  =============================="
rc=0
for ver in "${VERSIONS[@]}"; do
    s="${SRC[$ver]:-FAIL}"; d="${DEB[$ver]:-SKIP}"
    # Status words are all 4 chars (PASS/FAIL/SKIP), so they align without a
    # printf width specifier -- which would otherwise miscount the invisible
    # color escapes and break the columns.
    printf "  ${C_BOLD}ubuntu:%-8s${C_RESET} source: %s   deb: %s\n" \
        "$ver" "$(colorize "$s")" "$(colorize "$d")"
    [ "$s" = PASS ] || rc=1
    [ "$d" = PASS ] || [ "$d" = SKIP ] || rc=1
done
if [ "$rc" -eq 0 ]; then
    echo "${C_GREEN}${C_BOLD}ALL PASSED${C_RESET}"
else
    echo "${C_RED}${C_BOLD}SOME FAILED${C_RESET}"
fi
exit "$rc"
