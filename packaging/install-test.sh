#!/bin/sh
# Install the freshly built .debs into a clean Debian container and check the
# result. Run by .github/workflows/deb.yml as:
#   docker run --rm -v "$PWD/built-debs:/debs:ro" -v "$PWD/packaging:/packaging:ro" \
#     debian:<suite> sh /packaging/install-test.sh
set -eux

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y /debs/nfsroot-watchdog_*.deb /debs/nfsroot-watchdog-server_*.deb

# busybox-static came in as a dependency.
dpkg -s busybox-static | grep -qx 'Status: install ok installed'

# Units are enabled; nothing tried to start the arm unit (no systemd here,
# and it must only ever run at boot anyway).
test -L /etc/systemd/system/multi-user.target.wants/nfsroot-watchdog-arm.service
test -L /etc/systemd/system/timers.target.wants/nfsroot-watchdog.timer
test -f /etc/default/nfsroot-watchdog

# This container's root is not NFS: the watchdog must stand down, not arm.
/usr/lib/nfsroot-watchdog/nfsroot-watchdog-arm | tee /tmp/arm.out
grep -q 'not an NFS root' /tmp/arm.out
test ! -e /run/nfsroot-watchdog/check

# Arm against a fake NFS root and run real checks with the installed files
# and busybox-static.
mkdir -p /tmp/lower/etc/nfsroot-watchdog
# No stagger, so the scheduled reboot is due at once and the warning goes
# out on the first check; CONSOLE is a file so the broadcast can be read.
cat >>/etc/default/nfsroot-watchdog <<'CONF'
LOWER=/tmp/lower
KMSG=/tmp/kmsg
CONSOLE=/tmp/console
BASE_DELAY=0
SPACING=0
JITTER=0
CONF
/usr/lib/nfsroot-watchdog/nfsroot-watchdog-arm
/run/nfsroot-watchdog/bin/busybox sh /run/nfsroot-watchdog/check | tee /tmp/check0.out
test ! -s /tmp/check0.out
nfsroot-watchdog status

# An update that changes the root bumps the generation. The next check
# schedules this machine's reboot WARN_BEFORE (5 min) away and broadcasts the
# warning, with how to stop it; the test stops before the reboot is due.
nfsroot-generation run /tmp/lower -- sh -c 'echo new > /tmp/lower/etc/new-file'
cat /tmp/lower/etc/nfsroot-watchdog/generation
/run/nfsroot-watchdog/bin/busybox sh /run/nfsroot-watchdog/check | tee /tmp/check1.out
grep -q 'generation changed' /tmp/check1.out
grep -q '^<4>\*\*\* nfsroot-watchdog: .* will REBOOT in about 5 minutes' /tmp/check1.out
test -s /run/nfsroot-watchdog/deadline
cat /tmp/console
grep -q 'will REBOOT in about 5 minutes' /tmp/console
grep -q 'To STOP this reboot:  sudo nfsroot-watchdog inhibit' /tmp/console
grep -q 'To STOP this reboot' /tmp/kmsg
nfsroot-watchdog status | tee /tmp/status.out
grep -q '^warning sent:' /tmp/status.out

# Following the instructions stops it, and says so.
nfsroot-watchdog inhibit
/run/nfsroot-watchdog/bin/busybox sh /run/nfsroot-watchdog/check
grep -q 'is CANCELLED: local inhibit' /tmp/console
test ! -e /run/nfsroot-watchdog/deadline
echo "install test passed"
