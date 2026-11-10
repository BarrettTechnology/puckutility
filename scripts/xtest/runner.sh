#!/usr/bin/env bash
# Run scripts/test-cross-ubuntu.sh on request from a sandboxed agent that has no
# container runtime of its own. Start this OUTSIDE the sandbox, in a terminal
# you keep open:
#
#   scripts/xtest/runner.sh ~/code/p4gui/puckutility ~/code/p4gui/pucktuner
#
# The agent asks for a run by writing one line to <repo>/build/xtest/request:
#
#   [--source-only] [--no-pip] [UBUNTU_VERSION ...]     e.g.  --no-pip 22.04
#
# Nothing else is accepted, and the line is never executed or echoed back. The
# results are written only to the state directory (below), never into the repo,
# so a symlink planted in the repo cannot redirect the runner's writes. Mount the
# state directory READ-ONLY into the sandbox so the agent can read them:
#
#   ro ~/.local/state/puck-xtest          (claude-box sandbox.conf)
#
#   <state>/<repo>/status       idle | waiting-approval | running | rejected | done rc=N
#   <state>/<repo>/latest.log   the newest run's log (live while it runs)
#
# What runs where:
#   * Containers run this repo's scripts/xtest/*.sh and setup scripts, which the
#     agent can change; rootless podman (the default here) keeps them in your
#     user's sub-uid range with the checkout mounted read-only.
#   * On the HOST, unsandboxed: test-cross-ubuntu.sh and build-linux-installer.sh.
#     The runner pins them: the first run, and any run after either changed,
#     shows you the diff here and waits for y. The run then uses the runner's
#     own snapshot of the checkout, so nothing can change between your y and
#     the run.
#   * This runner itself: review changes to it before you restart it.
#
# Env: CONTAINER (default podman), XTEST_STATE (default
# ~/.local/state/puck-xtest), XTEST_POLL seconds (default 5), XTEST_TIMEOUT
# (default 4h).

main() {
    set -uo pipefail
    umask 022
    local state="${XTEST_STATE:-$HOME/.local/state/puck-xtest}"
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/puck-xtest"
    local poll="${XTEST_POLL:-5}"
    export CONTAINER="${CONTAINER:-podman}"
    local pinned=(scripts/test-cross-ubuntu.sh scripts/build-linux-installer.sh)

    [ "$#" -gt 0 ] || set -- "$(cd "$(dirname "$0")/../.." && pwd)"
    command -v "$CONTAINER" >/dev/null || { echo "ERROR: $CONTAINER not found" >&2; return 1; }
    [ -t 0 ] || { echo "ERROR: run this in a terminal: approvals are read from it" >&2; return 1; }

    local repos=() names=() r n
    for r in "$@"; do
        r="$(cd "$r" 2>/dev/null && pwd -P)" || { echo "ERROR: no such repo: $1" >&2; return 1; }
        [ -f "$r/scripts/test-cross-ubuntu.sh" ] || { echo "ERROR: $r has no scripts/test-cross-ubuntu.sh" >&2; return 1; }
        n="$(basename "$r")"
        repos+=("$r"); names+=("$n")
        mkdir -p "$state/$n/approved" "$r/build/xtest"
        set_status "$state/$n" idle
    done
    mkdir -p "$cache"
    echo "xtest runner: CONTAINER=$CONTAINER, state $state"
    printf '  watching %s/build/xtest/request\n' "${repos[@]}"

    trap 'echo; echo "xtest runner: stopped"; exit 130' INT TERM
    local i args
    while :; do
        for i in "${!repos[@]}"; do
            r="${repos[$i]}"; n="${names[$i]}"
            args="$(take_request "$r/build/xtest/request")" || continue
            run_one "$r" "$n" "$state/$n" "$cache" "$args" "${pinned[@]}"
        done
        sleep "$poll"
    done
}

# Status file: written whole and renamed, in the runner's own state directory.
set_status() {
    printf '%s %s\n' "$(date -Is)" "$2" > "$1/.status.tmp" && mv -f "$1/.status.tmp" "$1/status"
}

# Print the validated arguments of a pending request and remove it; fail when
# there is none. Only a small regular file is read; a symlink is removed unread.
take_request() {
    local f="$1" line tok out=()
    [ -e "$f" ] || [ -L "$f" ] || return 1
    if [ -L "$f" ] || [ ! -f "$f" ]; then
        rm -f "$f"; echo "  request ignored: not a regular file" >&2; return 1
    fi
    line="$(timeout 2 head -c 200 "$f" | head -n 1 | tr -cd 'a-z0-9.\- ')"
    rm -f "$f"
    for tok in $line; do
        case "$tok" in
            --source-only|--no-pip) out+=("$tok") ;;
            [0-9][0-9].04|[0-9][0-9].10) out+=("$tok") ;;
            *) echo "  request rejected: unknown argument" >&2; return 1 ;;
        esac
    done
    echo "${out[*]}"
}

run_one() {
    local repo="$1" name="$2" st="$3" cache="$4" args="$5"; shift 5
    local pinned=("$@") ts snap log f changed=() ans rc
    ts="$(date +%Y%m%d-%H%M%S)"
    log="$st/$ts.log"
    echo "[$(date +%T)] $name: request '${args}'"

    # Private snapshot of the working tree (submodules included), outside the
    # sandbox. Symlinks are copied as symlinks, never followed.
    snap="$(mktemp -d "$cache/$name.$ts.XXXX")"
    tar -C "$repo" -cf - --exclude=.git --exclude=.venv --exclude=build \
        --exclude=./bin --exclude=./lib --exclude=./lib64 --exclude=./include \
        --exclude=./share --exclude=./pyvenv.cfg --exclude=./logs \
        --exclude=./data_dump --exclude='*.egg-info' --exclude=__pycache__ . \
        | tar -C "$snap" -xf - || { set_status "$st" "done rc=1 (snapshot failed)"; rm -rf "$snap"; return; }

    # The host-run scripts must be plain files in a plain scripts/ directory,
    # and identical to the approved copies.
    if [ -L "$snap/scripts" ] || [ ! -d "$snap/scripts" ]; then
        echo "  rejected: scripts/ is not a directory"; set_status "$st" rejected; rm -rf "$snap"; return
    fi
    for f in "${pinned[@]}"; do
        if [ -L "$snap/$f" ] || [ ! -f "$snap/$f" ]; then
            echo "  rejected: $f is not a regular file"; set_status "$st" rejected; rm -rf "$snap"; return
        fi
        cmp -s "$snap/$f" "$st/approved/$(basename "$f")" || changed+=("$f")
    done
    if [ "${#changed[@]}" -gt 0 ]; then
        set_status "$st" waiting-approval
        echo "  These host-run scripts are new or changed since you last approved them:"
        for f in "${changed[@]}"; do
            if [ -f "$st/approved/$(basename "$f")" ]; then
                diff -u --label "approved/$f" --label "$name/$f" \
                    "$st/approved/$(basename "$f")" "$snap/$f" | ${PAGER:-less -R}
            else
                echo "  (first run: $f, $(wc -l < "$snap/$f") lines)"
                ${PAGER:-less -R} "$snap/$f"
            fi
        done
        read -r -p "  Run them on this host? [y/N] " ans
        if [ "$ans" != y ] && [ "$ans" != Y ]; then
            echo "  rejected"; set_status "$st" rejected; rm -rf "$snap"; return
        fi
        for f in "${changed[@]}"; do cp "$snap/$f" "$st/approved/$(basename "$f")"; done
    fi

    set_status "$st" "running $ts ${args}"
    ln -sfn "$ts.log" "$st/latest.log"
    echo "  running, log $log"
    # shellcheck disable=SC2086  # args is validated tokens only
    ( cd "$snap" && NO_COLOR=1 TMPDIR="$cache" \
        timeout "${XTEST_TIMEOUT:-4h}" bash scripts/test-cross-ubuntu.sh $args ) \
        > "$log" 2>&1 < /dev/null
    rc=$?
    set_status "$st" "done rc=$rc $ts ${args}"
    echo "[$(date +%T)] $name: done rc=$rc"
    tail -n 8 "$log" | sed 's/^/  | /'
    rm -rf "$snap"
    # Keep the last 20 logs.
    ls -1t "$st"/*.log 2>/dev/null | grep -v latest.log | tail -n +21 | xargs -r rm -f
}

# Everything above is a function, so bash has parsed the whole file before it
# runs anything: an edit to this file takes effect only at the next start.
main "$@"
exit $?
