#!/usr/bin/env bash
# Cross-Ubuntu smoke test — tests BOTH halves of "works on 20.04 -> 26.04":
#
#   SOURCE : run THIS repo's scripts/setup-venv.sh (uv + standalone CPython 3.13
#            + host-matched wxPython wheel) and verify `import wx` + `wx.App()`
#            under a headless X server. This exercises the dev/from-source path,
#            which uses each container's OWN system GLib.
#   DEB    : build the installer (.run wrapping the PyInstaller-frozen app deb,
#            built on Ubuntu 20.04, and the puck-can-* adapter debs), run it
#            unattended in each clean container, check the CAN adapter options
#            (select, deselect, uninstall), and launch the installed binary.
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
    echo "==================  building installer (once)  =================="
    DEB_REL=""
    if ./scripts/build-linux-installer.sh --docker --installer; then
        DEB_REL="$(ls -t build/*-installer.run 2>/dev/null | head -1)"
    fi
    if [ -z "$DEB_REL" ] || [ ! -f "$DEB_REL" ]; then
        echo "ERROR: installer build failed / not found — marking DEB tests SKIP" >&2
        for ver in "${VERSIONS[@]}"; do DEB["$ver"]=SKIP; done
    else
        echo "Built: $DEB_REL"
        for ver in "${VERSIONS[@]}"; do
            echo "==================  DEB  ubuntu:${ver}  =================="
            # Run ONLY the installer in a bare container: apt resolves and pulls
            # the packages' declared Depends (the GTK/X11 runtime stack,
            # can-utils, dfu-util) from the repo. This VALIDATES that the debs are
            # self-sufficient on a minimal system -- if a runtime lib is missing
            # from Depends, the launch below crashes and names it.
            if docker run --rm \
                    -v "$REPO:/src:ro" \
                    -e DEBIAN_FRONTEND=noninteractive \
                    "ubuntu:${ver}" bash -euo pipefail -c "
                        has() { dpkg-query -W -f='\${Status}' \"\$1\" 2>/dev/null | grep -q 'ok installed'; }
                        R='/src/${DEB_REL}'
                        echo '  installer: both adapters'
                        bash \"\$R\" --yes >/tmp/inst.out 2>&1 || { cat /tmp/inst.out; exit 1; }
                        for f in /usr/lib/udev/rules.d/60-can.rules /usr/lib/udev/rules.d/61-can-up.rules \\
                                 /usr/lib/udev/rules.d/90-canable.rules /usr/lib/systemd/system/can-up@.service \\
                                 /etc/NetworkManager/conf.d/99-puck-can.conf /usr/bin/dfu-util /usr/bin/candump; do
                            [ -e \"\$f\" ] || { echo \"  missing \$f\"; exit 1; }
                        done
                        echo '  installer: deselect Peak'
                        bash \"\$R\" --yes --no-peak >/tmp/inst.out 2>&1 || { cat /tmp/inst.out; exit 1; }
                        ! has puck-can-peak || { echo '  puck-can-peak still installed'; exit 1; }
                        has puck-can-canable || { echo '  puck-can-canable was removed'; exit 1; }
                        echo '  installer: re-add Peak'
                        bash \"\$R\" --yes >/tmp/inst.out 2>&1 || { cat /tmp/inst.out; exit 1; }
                        has puck-can-peak || { echo '  puck-can-peak not reinstalled'; exit 1; }
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
                        echo '  installer: uninstall'
                        bash \"\$R\" --yes --uninstall >/tmp/inst.out 2>&1 || { cat /tmp/inst.out; exit 1; }
                        for p in puckutilityapp puck-can-common puck-can-peak puck-can-canable; do
                            ! has \"\$p\" || { echo \"  \$p still installed\"; exit 1; }
                        done
                        [ ! -e /etc/NetworkManager/conf.d/99-puck-can.conf ] || { echo '  NM conf left behind'; exit 1; }
                        echo '  uninstall OK'
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
