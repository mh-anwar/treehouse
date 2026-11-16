#!/usr/bin/env bash
# Fresh-install test in throwaway containers that boot systemd: dry run, install, a friend, idempotent
# re-install, update from git, uninstall, reinstall (tests/container/inside.sh). Nothing on the host changes.
# Works with rootless Docker or Podman; needs no root on the host.
#
#   tests/container/run.sh                     Ubuntu 24.04 and Debian 13
#   tests/container/run.sh ubuntu:24.04        one image
#   tests/container/run.sh archlinux:latest    Arch Linux (Dockerfile.arch; any other base uses Dockerfile)
#   FROM=v1 tests/container/run.sh             start on an older commit, then update to this tree (a live box's path)
#   KEEP=1 tests/container/run.sh ...          leave the container running to poke at (docker exec -it ...)
set -uo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
ENGINE=${ENGINE:-$(command -v docker || command -v podman)}
[[ -n $ENGINE ]] || { echo "needs docker or podman" >&2; exit 2; }
if [[ $(basename "$ENGINE") == docker && -z ${DOCKER_HOST:-} && -S /run/user/$(id -u)/docker.sock ]]; then
    export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock   # rootless docker
fi
BASES=("$@"); [[ ${#BASES[@]} -gt 0 ]] || BASES=(ubuntu:24.04 debian:trixie)
rc=0
for base in "${BASES[@]}"; do
    tag="treehouse-test:${base//[:\/]/-}" name="treehouse-test-${base//[:\/.]/-}-$$"
    dockerfile=Dockerfile; [[ $base == *arch* ]] && dockerfile=Dockerfile.arch
    echo "### $base"
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
    "$ENGINE" exec "$name" bash /src/tests/container/inside.sh || rc=1
    if [[ -n ${KEEP:-} ]]; then echo "kept: $ENGINE exec -it $name bash"; else "$ENGINE" rm -f "$name" >/dev/null; fi
done
exit $rc
