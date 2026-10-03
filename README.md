# nfsroot-watchdog

Reboot NFS-root clients whose root has changed underneath them, one at a
time, and only once the change is finished.

## The problem

A machine that boots from an NFS root (read-only, usually with an
[overlayroot](https://packages.debian.org/overlayroot) tmpfs on top) caches
handles to the files it has used. When the server replaces one of those files,
the old inode is gone. That happens when dpkg upgrades a package, when dpkg
rewrites `/var/lib/dpkg/status` (which `dpkg --configure -a` does even when
there is nothing to configure), and when a config-management tool writes a
temp file and renames it into place. From then on the client gets
`Stale file handle` (ESTALE) for that file.

Everything already running carries on, so the fleet looks healthy. Whatever
opens a replaced file fails. On one fleet it was `/home/pi/.ssh/authorized_keys`,
so key-based ssh broke on every board at once, and `dpkg-query` broke with it.
The only fix is a reboot, and rebooting a whole fleet at once, or while the
update is still running, is its own outage.

## How it works

Two packages, one small protocol inside the exported root:

| file in the root | written by | meaning |
|---|---|---|
| `/etc/nfsroot-watchdog/update.lock` | `nfsroot-generation begin` | an update is in progress: clients hold still |
| `/etc/nfsroot-watchdog/generation` | `nfsroot-generation end` | `<epoch> <iso-time> <reason>`, replaced whenever an update changed the root |
| `/etc/nfsroot-watchdog/version` | the server, where each version of the root has its own directory | the version's name; the marker is then `<epoch> <iso-time> <current version>` in every version |
| `/etc/nfsroot-watchdog/inhibit` | a person | no client reboots itself while this exists |

**`nfsroot-watchdog`** (install in the client root) runs a check once a
minute (`nfsroot-watchdog.timer`). It reads the lock and the generation
through the NFS mount itself: overlayroot's lower layer `/media/root-ro`, or `/`
for a plain NFS root. That view sees the server's new files. The client
reboots when:

1. **the generation differs** from the one it booted with. If the root it
   booted has a `version` file, the test is instead that the generation's
   third field names a different version: a machine already on the current
   version never reboots, even after a rollback to it; or
2. **probe files stay stale** (`/var/lib/dpkg/status`, `/etc/ld.so.cache`, the
   generation marker, plus any you add) on two checks in a row, *and* the root
   has been quiet for an hour. This catches updates made without
   `nfsroot-generation`.

3. **the NFS mount itself is stale**, on two checks in a row. That happens
   when the server replaces the whole exported directory rather than files
   inside it. Nothing on the mount, not even the lock, can be read after
   that, so this trigger only waits for the confirmation. The stagger, the
   warning and the local inhibit still apply.

Otherwise it never reboots while the lock or an inhibit file exists. It never counts a
read error other than ESTALE as a change. It waits up to an hour for logged-in
users.

**Warning.** At least five minutes (`WARN_BEFORE`) before rebooting, it
broadcasts a warning to `/dev/console`, to every terminal `who` lists, to the
journal (at warning priority) and to the kernel log. The warning says when
the reboot will happen, why, and how to stop it:

```
*** nfsroot-watchdog: node33 will REBOOT in about 6 minutes, at 03:25:48 UTC ***
Why: the NFS root this machine booted from has changed on the server.
     Files that were replaced there fail here with "Stale file handle"
     until the machine reboots. (generation changed: ...)
To STOP this reboot:  sudo nfsroot-watchdog inhibit
     (or: sudo touch /run/nfsroot-watchdog.inhibit). It stays stopped until the next boot;
     undo with: sudo nfsroot-watchdog release
To see the plan:      nfsroot-watchdog status
```

A warned reboot never happens sooner than `WARN_BEFORE` after the warning,
even if the clock jumps. If it is called off (an inhibit, a new update lock,
the root becoming consistent again), that is broadcast too. Each terminal
write is bounded by a timeout, so a terminal nobody reads cannot delay the
reboot. `DRY_RUN=1` logs the warning but writes to no terminal.

**Staggering.** Each client reboots at `generation time + BASE_DELAY + slot ×
SPACING` (420 s + slot × 20 s by default, plus up to 10 s of jitter; the base
delay leaves room for noticing the change and for the five-minute warning). Every
client counts from the same server timestamp, so clients with distinct slots
never reboot together, however late each one notices. The slot comes from the
hostname:
- `SLOT=` fixes it per machine.
- `SLOT_SED=` maps hostnames such as `pi-sw2-p33` onto a switch/port
  placement.
- Otherwise the hostname's trailing number is used (`node07` is slot 7), or
  failing that a hash.

**Surviving a stale root.** At boot, `nfsroot-watchdog-arm` copies the check
script, its resolved configuration and a **static** busybox into `/run`. From
then on the check runs only from there, so a replaced libc, shell or config
file cannot stop it. It reboots with `systemctl reboot` if `systemctl` and
`systemd-shutdown` are themselves still intact. If PID 1 cannot exec
`systemd-shutdown`, it freezes instead of rebooting, so when either is stale the
check uses `reboot -f` instead. Each decision also goes to the kernel log, which
with netconsole outlives the reboot.

**`nfsroot-watchdog-server`** (install on the machine that updates the root)
provides `nfsroot-generation`:

```sh
nfsroot-generation begin /srv/nfs/root     # take the lock (kept if already there)
chroot /srv/nfs/root apt-get -y dist-upgrade
nfsroot-generation end /srv/nfs/root       # bump if anything changed, then unlock

# or, the same in one go; a failing command leaves the lock in place:
nfsroot-generation run /srv/nfs/root -- chroot /srv/nfs/root apt-get -y dist-upgrade
```

`end` counts a change as any file or symlink with a ctime not older than the
lock's. It uses ctime, not mtime, because dpkg unpacks files with old mtimes and
`rsync -t` / `cp -p` copy them. Directories don't count, and build scratch,
caches and logs are ignored (`--ignore` adds more). An update that changed
nothing doesn't reboot anyone. If an update fails part-way, the lock stays: the
clients keep running on the old root rather than reboot into a half-built one.
The next successful update publishes both.

**One directory per version.** Updating a root in place always leaves some
clients on stale files until they reboot. A server can avoid that by never
changing a tree a client runs from. It keeps each version of the root in its
own directory and points a `current` symlink at the one clients should boot:

```
/srv/nfs/rpi/versions/<name>/{boot,root}   one entry per version (or a symlink to one)
/srv/nfs/rpi/current -> versions/<name>    what a client gets when it boots
```

Build the new version elsewhere on the same filesystem, rename it into
`versions/`, then:

```sh
nfsroot-generation publish  /srv/nfs/rpi <name>   # make it current
nfsroot-generation rollback /srv/nfs/rpi <name>   # make an earlier one current again
nfsroot-generation list     /srv/nfs/rpi          # every version, current, published, inhibited (JSON)
nfsroot-generation inhibit  /srv/nfs/rpi [--reason TEXT]   # hold every client's self-reboot
nfsroot-generation uninhibit /srv/nfs/rpi
```

`publish` writes `version` (the entry's name, once) and a marker naming the new
version into it. Then it swaps `current`, then writes the same marker into every
other published version. Clients on an older version see a marker naming
another version and reboot, staggered as usual. Clients on the current version
stay put, also after a `rollback`. Running `publish` again is safe: it finishes
a publish that died part-way. An entry that was never published is never
written. The fleet inhibit lives in each version, so use `inhibit` and
`uninhibit` rather than `touch`, and `publish` carries it to the new version.
Note the two tools: `nfsroot-generation inhibit BASE`, on the server, holds
**every** client; `nfsroot-watchdog inhibit`, on a client, holds only that one
machine, until its next boot.
These commands wait for each other (a `flock` on
`/srv/nfs/rpi/nfsroot-generation.lock`). Every write is a temp file and a
rename, so versions that share identical files by hard link
(`rsync --link-dest`) are never changed through each other. Old versions are
kept until you delete them; never delete one a client still runs.

## Install

The packages are published as a signed apt repository per Debian suite
(bookworm, trixie, forky, sid; `Architecture: all`): put your suite's name in
place of `trixie` below. Use the `fpgas.online` URL: the
`fpgas-online.github.io` one redirects to it over plain http, which apt will
not follow.

```sh
sudo install -d -m0755 /etc/apt/keyrings
curl -fsSL https://fpgas.online/nfsroot-watchdog/nfsroot-watchdog.gpg \
  | sudo tee /etc/apt/keyrings/nfsroot-watchdog.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/nfsroot-watchdog.gpg] https://fpgas.online/nfsroot-watchdog/trixie/ ./" \
  | sudo tee /etc/apt/sources.list.d/nfsroot-watchdog.list
sudo apt update

# in the client root (e.g. from a chroot):
sudo apt install nfsroot-watchdog
# on the server:
sudo apt install nfsroot-watchdog-server
```

The repository's signing key is
`FC0E 4835 E563 E003 D06C  F358 A105 FA06 79EC 6204`
(`gpg --show-keys /etc/apt/keyrings/nfsroot-watchdog.gpg` shows it).

`nfsroot-watchdog` depends on `busybox-static`, which replaces Debian's
dynamically linked `busybox` package. On a machine whose root is not NFS it
installs but stays idle.

A client picks the watchdog up at its next boot. So after the first update that
installs it, reboot the fleet once by hand (staggered, as you would have
anyway). From then on it looks after itself.

## Configure and operate

All client settings are in `/etc/default/nfsroot-watchdog`, each documented
with its default. They are read once per boot.

```sh
nfsroot-watchdog status     # what it sees: root, slot, generations, lock, deadline
nfsroot-watchdog inhibit    # keep this machine up until its next boot (or release)
nfsroot-watchdog release
journalctl -u nfsroot-watchdog -u nfsroot-watchdog-arm
```

Three lines of `status` are a stable interface for other tools: `NFS root:`,
`root generation:` and (only where the root has a version file) `version:`.
Each is the label, a colon, spaces and the value, alone on its line.

To stop the whole fleet, `touch <root>/etc/nfsroot-watchdog/inhibit` on the
server. `nfsroot-generation status <root>` shows the lock and the generation as
the clients see them.

## Development

```sh
NFSROOT_WATCHDOG_TEST_BUSYBOX=/bin/busybox \
NFSROOT_WATCHDOG_TEST_STATIC_BUSYBOX=/path/to/unpacked/busybox-static/busybox \
python3 -m pytest tests/
```

CI builds with [mithro/apt-repo-action](https://github.com/mithro/apt-repo-action)'s
`build-deb` action, which can only run in GitHub Actions. The same build by
hand, with a checkout of apt-repo-action's `main` next to this one:

```sh
docker run --rm -v "$PWD:/src" -v "$PWD/../apt-repo-action:/apt-repo-action:ro" -w /src \
  debian:bookworm bash -ec '
    apt-get update
    apt-get install -y --no-install-recommends \
      build-essential ca-certificates debhelper dpkg-dev fakeroot git python3
    apt-get build-dep -y ./
    git config --global --add safe.directory "*"
    python3 /apt-repo-action/scripts/deb-version.py --suite bookworm --write-changelog
    dpkg-buildpackage -us -uc -A
    mkdir -p built-debs && cp ../*.deb built-debs/'
docker run --rm --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
  --tmpfs /run:rw,noexec,nosuid,nodev \
  -v "$PWD/built-debs:/debs:ro" -v "$PWD/packaging:/packaging:ro" \
  debian:bookworm sh /packaging/install-test.sh
```

The version comes from `git describe` plus the suite's `~deb<R>`
(`0.0.post28~deb12`). There is no committed `debian/changelog`: the build
writes one with just its own entry, and git ignores it. The install test wants
`/run` mounted noexec, as an initramfs-booted root has it.

The logic tests need Debian's dynamic `busybox` package, because the static
build runs its own applets and ignores the fakes on `PATH`. The `Debian
packages` workflow runs them on bookworm, trixie and forky, then builds and
install-tests for every suite and (from `main`) publishes the packages.

## License

Apache 2.0.
