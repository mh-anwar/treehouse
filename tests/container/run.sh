#!/usr/bin/env bash
# Tests in throwaway containers that boot systemd. Nothing on the host changes. Works with rootless Docker or
# Podman; needs no root on the host. SCENARIO picks what runs inside:
#   install (default)  dry run, install, a friend, idempotent re-install, update from git, uninstall, reinstall
#                      (inside.sh)
#   agents             a full install with the coding agents and toolchains, then checks as a friend that every
#                      advertised tool runs and installs into their home without sudo (scenario-agents.sh; downloads
#                      a few GB, 10-15 min)
#   t3switch           `box t3 switch` moves the owner's T3 Code from T3's own t3code.service to the box: waits
#                      while an agent works, rolls back when the new one fails, then switches (scenario-t3switch.sh,
#                      with a stand-in t3)
#
#   tests/container/run.sh                     Ubuntu 24.04 and Debian 13
#   tests/container/run.sh ubuntu:24.04        one image
#   tests/container/run.sh archlinux:latest    Arch Linux (Dockerfile.arch; any other base uses Dockerfile)
#   SCENARIO=agents tests/container/run.sh     a scenario other than install (agents and t3switch: Debian 13 only)
#   FROM=v1 tests/container/run.sh             start on an older commit, then update to this tree (a live box's path)
#   KEEP=1 tests/container/run.sh ...          leave the container running to poke at (docker exec -it ...)
set -uo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
ENGINE=${ENGINE:-$(command -v docker || command -v podman)}
[[ -n $ENGINE ]] || { echo "needs docker or podman" >&2; exit 2; }
if [[ $(basename "$ENGINE") == docker && -z ${DOCKER_HOST:-} && -S /run/user/$(id -u)/docker.sock ]]; then
    export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock   # rootless docker
fi
SCENARIO=${SCENARIO:-install}
SCRIPT=scenario-$SCENARIO.sh; [[ $SCENARIO == install ]] && SCRIPT=inside.sh
[[ -f $REPO/tests/container/$SCRIPT ]] || { echo "no such scenario: $SCENARIO (tests/container/$SCRIPT)" >&2; exit 2; }
BASES=("$@")
if [[ ${#BASES[@]} == 0 ]]; then
    if [[ $SCENARIO == install ]]; then BASES=(ubuntu:24.04 debian:trixie); else BASES=(debian:trixie); fi
fi
rc=0 name=
# an interrupted run still removes its own container (only that one, by name)
trap '[[ -n $name && -z ${KEEP:-} ]] && "$ENGINE" rm -f "$name" >/dev/null 2>&1' EXIT
trap 'exit 130' INT TERM
for base in "${BASES[@]}"; do
    tag="treehouse-test:${base//[:\/]/-}" name="treehouse-test-$SCENARIO-${base//[:\/.]/-}-$$"
    dockerfile=Dockerfile; [[ $base == *arch* ]] && dockerfile=Dockerfile.arch
    echo "### $base ($SCENARIO)"
    "$ENGINE" build -q --build-arg BASE="$base" -f "$REPO/tests/container/$dockerfile" -t "$tag" "$REPO/tests/container" >/dev/null || { echo "build failed"; rc=1; continue; }
    "$ENGINE" run -d --name "$name" --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
        --hostname testbox "$tag" >/dev/null || { echo "run failed"; rc=1; continue; }
    for _ in $(seq 60); do
        s=$("$ENGINE" exec "$name" systemctl is-system-running 2>/dev/null); [[ $s == running || $s == degraded ]] && break; sleep 1
    done
    # the committed tree plus uncommitted edits, without .git or ignored files
    (cd "$REPO" && git ls-files -co --exclude-standard -z | tar --null -T - -cf -) | "$ENGINE" exec -i "$name" sh -c 'mkdir -p /src && tar -x -C /src'
    if [[ -n ${FROM:-} ]]; then
        (cd "$REPO" && git archive "$FROM") | "$ENGINE" exec -i "$name" sh -c 'mkdir -p /old && tar -x -C /old' \
            || { echo "can't export $FROM"; rc=1; }
    fi
    "$ENGINE" exec "$name" bash "/src/tests/container/$SCRIPT" || rc=1
    if [[ -n ${KEEP:-} ]]; then echo "kept: $ENGINE exec -it $name bash"; else "$ENGINE" rm -f "$name" >/dev/null; fi
    name=
done
exit $rc
