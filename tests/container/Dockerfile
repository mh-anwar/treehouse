# A throwaway Debian/Ubuntu box with systemd as PID 1, for tests/container/run.sh.
# Only what a fresh server install has (sshd, cron, acl) plus python3 and git; no Tailscale.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv libpam-systemd dbus python3 openssh-server \
      openssh-client git curl ca-certificates acl cron at util-linux procps iproute2 passwd \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && systemctl mask getty@tty1.service sys-kernel-config.mount sys-kernel-debug.mount sys-kernel-tracing.mount \
      proc-sys-fs-binfmt_misc.automount systemd-binfmt.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
