"""Tests for the nfsroot-watchdog client (src/nfsroot-watchdog-*).

nfsroot-watchdog-check decides, once a minute on every NFS-root client,
whether to reboot. A wrong "yes" reboots machines into a half-built root or
takes a fleet down in one go; a wrong "no" leaves machines on dead file
handles until someone power-cycles them. So the decision is tested here
against a synthetic NFS root rather than only exercised on real fleets.

The logic tests run the real scripts under a NON-standalone busybox (the
Debian `busybox` package) so that `date`, `stat`, `who` and `reboot` can be
replaced with fakes on PATH; a standalone build runs its own applets and
ignores PATH. test_static_busybox_smoke runs the production arrangement,
Debian's static busybox, with no fakes at all.

    NFSROOT_WATCHDOG_TEST_BUSYBOX=/usr/bin/busybox \\
    NFSROOT_WATCHDOG_TEST_STATIC_BUSYBOX=/path/to/static/busybox \\
    pytest tests/
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
CHECK = SRC / "nfsroot-watchdog-check"
ARM = SRC / "nfsroot-watchdog-arm"
CLI = SRC / "nfsroot-watchdog"
DEFAULT_CONFIG = REPO / "debian/nfsroot-watchdog.default"

BUSYBOX = os.environ.get("NFSROOT_WATCHDOG_TEST_BUSYBOX") or shutil.which("busybox")
STATIC_BUSYBOX = os.environ.get("NFSROOT_WATCHDOG_TEST_STATIC_BUSYBOX")

GEN = "/etc/nfsroot-watchdog/generation"
LOCK = "/etc/nfsroot-watchdog/update.lock"
FLEET_INHIBIT = "/etc/nfsroot-watchdog/inhibit"
T0 = 1_790_000_000  # "now" at boot in the fake clock

# The fpgas.online welland placement scheme, as an example of SLOT_SED.
PLACEMENT = "\n".join(
    [
        "SLOT_SED='s/^pi-sw([0-9]+)-p([0-9]+)$/\\1 \\2/'",
        "SLOT_STRIDE=48",
        "SLOT_BASE=49",
    ]
)


def _standalone(busybox):
    """True if this busybox runs its own applets in preference to PATH."""
    r = subprocess.run(
        [busybox, "sh", "-c", "head -c0 /dev/null"],
        env={"PATH": "/nonexistent"},
        capture_output=True,
    )
    return r.returncode == 0


needs_busybox = pytest.mark.skipif(
    BUSYBOX is None or _standalone(BUSYBOX),
    reason="needs a non-standalone busybox (Debian's `busybox` package)",
)


class Client:
    """One fake NFS-root client: NFS lower, running root, state dir, fakes."""

    def __init__(self, tmp_path, busybox, hostname="node33", fakes=True, config=""):
        self.busybox = busybox
        self.lower = tmp_path / "lower"
        self.root = tmp_path / "root"
        self.state = tmp_path / "run/nfsroot-watchdog"
        self.inhibit = tmp_path / "run/nfsroot-watchdog.inhibit"
        self.calls = tmp_path / "calls"
        self.kmsg = tmp_path / "kmsg"
        self.hostname = tmp_path / "hostname"
        self.console = tmp_path / "console"
        self.dev = tmp_path / "dev"
        self.fakes = fakes
        for d in (self.lower, self.root):
            (d / "etc/nfsroot-watchdog").mkdir(parents=True)
            (d / "var/lib/dpkg").mkdir(parents=True)
            (d / "var/lib/dpkg/status").write_text("Package: x\n")
        self.hostname.write_text(hostname + "\n")
        self.calls.write_text("")
        self.kmsg.write_text("")
        self.console.write_text("")
        (self.dev / "pts").mkdir(parents=True)

        sbin = tmp_path / "sbin"
        sbin.mkdir()
        self.systemctl = sbin / "systemctl"
        self.systemd_shutdown = sbin / "systemd-shutdown"
        self._script(self.systemctl, f'echo "systemctl $*" >>"{self.calls}"')
        self._script(self.systemd_shutdown, "exit 0")

        # The shipped, all-commented /etc/default file, then this test's
        # settings: the same layering a real install has.
        self.config = tmp_path / "default"
        self.config.write_text(
            DEFAULT_CONFIG.read_text()
            + "\n".join(
                [
                    "",
                    f"LOWER={self.lower}",
                    f"ROOT={self.root}",
                    f"INHIBIT={self.inhibit}",
                    f"HOSTNAME_FILE={self.hostname}",
                    f"KMSG={self.kmsg}",
                    f"CONSOLE={self.console}",
                    f"DEV_DIR={self.dev}",
                    f"SYSTEMCTL={self.systemctl}",
                    f"SYSTEMD_SHUTDOWN={self.systemd_shutdown}",
                    "QUIET_DIRS=/var/lib/dpkg",
                    "JITTER=0",
                    "USER_GRACE=600",
                    config,
                    "",
                ]
            )
        )
        self.now = T0

    def _script(self, path, body):
        path.write_text(f"#!{self.busybox} sh\n{body}\n")
        path.chmod(0o755)

    def env(self):
        return {"NFSROOT_WATCHDOG_STATE": str(self.state), "PATH": "/nonexistent"}

    def arm(self, mounts=None):
        env = {
            **self.env(),
            "PATH": str(Path(self.busybox).parent) + ":/usr/bin:/bin",
            "NFSROOT_WATCHDOG_BUSYBOX": self.busybox,
            "NFSROOT_WATCHDOG_LIBDIR": str(SRC),
            "NFSROOT_WATCHDOG_CONFIG": str(self.config),
        }
        if mounts is not None:
            m = self.config.parent / "mounts"
            m.write_text(mounts)
            env["NFSROOT_WATCHDOG_MOUNTS"] = str(m)
        r = subprocess.run([self.busybox, "sh", str(ARM)], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        if self.fakes and (self.state / "check").exists():
            self._install_fakes()
        return r.stdout

    def _install_fakes(self):
        b = self.state / "bin"
        for name in ("date", "stat", "who", "reboot"):
            (b / name).unlink()
        s = self.state
        # The fake clock for "now"; formatting a given time (-d) is real.
        self._script(
            b / "date",
            f'case "$*" in *-d*) exec "{s}/bin/busybox" date "$@" ;; esac; cat "{s}/fake-now"',
        )
        self._script(b / "who", f'[ -e "{s}/fake-who" ] && cat "{s}/fake-who"; exit 0')
        self._script(b / "reboot", f'echo "reboot $*" >>"{self.calls}"')
        # ESTALE for any path listed in fake-stale, else the real stat.
        self._script(
            b / "stat",
            f"""for a in "$@"; do
    if grep -qxF -e "$a" "{s}/fake-stale"; then
        echo "stat: can't stat '$a': Stale file handle" >&2
        exit 1
    fi
done
exec "{s}/bin/busybox" stat "$@\"""",
        )
        (s / "fake-stale").write_text("")
        self.set_now(self.now)

    def set_now(self, now):
        self.now = now
        if self.fakes:
            (self.state / "fake-now").write_text(f"{now}\n")

    def _run(self, *args):
        return subprocess.run(
            [str(self.state / "bin/busybox"), "sh", str(self.state / "check"), *args],
            env=self.env(),
            capture_output=True,
            text=True,
        )

    def check(self):
        r = self._run()
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def slot(self, name):
        r = self._run("slot", name)
        assert r.returncode == 0, r.stderr
        return int(r.stdout)

    def write_lower(self, rel, content):
        p = self.lower / rel.lstrip("/")
        # Replace, never rewrite in place: what dpkg and most tools do.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(p)
        # QUIET_DIRS mtimes are compared with the fake clock.
        os.utime(p.parent, (self.now, self.now))

    def age_lower_dirs(self, age):
        t = self.now - age
        os.utime(self.lower / "var/lib/dpkg", (t, t))

    def stale(self, *paths):
        """Make stat answer ESTALE. Paths inside the client's running root
        are given as the client sees them (/var/lib/dpkg/status); absolute
        test paths (the lower, the fake systemd binaries) as they are."""
        base = str(self.root.parent)
        full = [p if p.startswith(base) else f"{self.root}{p}" for p in paths]
        (self.state / "fake-stale").write_text("".join(f"{p}\n" for p in full))

    def deadline(self):
        f = self.state / "deadline"
        return int(f.read_text()) if f.exists() else None

    def rebooted(self):
        return [c for c in self.calls.read_text().splitlines() if "reboot" in c]

    def warned_at(self):
        f = self.state / "warned"
        return int(f.read_text()) if f.exists() else None

    def run_until_reboot(self, limit=20):
        """Check once a minute of fake time until the machine reboots."""
        for _ in range(limit):
            self.check()
            if self.rebooted():
                return self.now
            self.set_now(self.now + 60)
        raise AssertionError("no reboot within the limit")


@pytest.fixture
def client(tmp_path):
    c = Client(tmp_path, BUSYBOX)
    c.write_lower(GEN, f"{T0 - 86400} 2026-09-23T00:00:00Z\n")
    c.arm()
    return c


# --- arming ------------------------------------------------------------------


@needs_busybox
def test_arm_records_boot_state(client):
    assert (client.state / "boot-generation").read_text().startswith(str(T0 - 86400))
    present = (client.state / "probes-present").read_text().split()
    assert "/var/lib/dpkg/status" in present
    probes = (client.state / "config").read_text().split("PROBES='")[1].split("'")[0].split()
    assert GEN in probes  # always added to PROBES
    assert "/etc/ld.so.cache" not in present  # a default probe, absent here


@needs_busybox
def test_config_is_resolved_into_run(client):
    # The check never reads /etc: everything it needs is in /run.
    conf = (client.state / "config").read_text()
    assert f"LOWER='{client.lower}'" in conf
    assert "SLOTS='96'" in conf  # a built-in default


@needs_busybox
@pytest.mark.parametrize(
    "mounts,armed",
    [
        # overlayroot over NFS: watch the lower layer
        ("overlayroot / overlay rw 0 0\nsrv:/r /media/root-ro nfs ro 0 0\n", "/media/root-ro"),
        # plain read-only NFS root
        ("srv:/r / nfs4 ro 0 0\n", "/"),
        # a local disk: nothing to watch
        ("/dev/sda1 / ext4 rw 0 0\n", None),
    ],
)
def test_arm_finds_the_nfs_root(tmp_path, mounts, armed):
    c = Client(tmp_path, BUSYBOX)
    text = c.config.read_text().replace(f"LOWER={c.lower}", "LOWER=auto")
    c.config.write_text(text)
    out = c.arm(mounts=mounts)
    if armed is None:
        assert "not an NFS root" in out
        assert not (c.state / "check").exists()
    else:
        assert f"NFS root at '{armed}'" in out
        assert (c.state / "check").exists()


@needs_busybox
def test_disabled(tmp_path):
    c = Client(tmp_path, BUSYBOX, config="ENABLED=0")
    assert "disabled" in c.arm()
    assert not (c.state / "check").exists()


# --- stagger slots -------------------------------------------------------


@needs_busybox
@pytest.mark.parametrize(
    "name,slot",
    [
        ("node1", 1),
        ("node33", 33),
        ("node08", 8),  # "08" is bad octal to shell arithmetic
        ("rack2-node107", 107 % 96),
    ],
)
def test_slot_from_trailing_number(client, name, slot):
    assert client.slot(name) == slot


@needs_busybox
def test_slot_hash_fallback_is_stable_and_in_range(client):
    a = client.slot("buildhost")
    assert a == client.slot("buildhost")
    assert 0 <= a < 96


@needs_busybox
def test_fixed_slot(tmp_path):
    c = Client(tmp_path, BUSYBOX, config="SLOT=7")
    c.arm()
    assert c.slot("node33") == 7


@needs_busybox
@pytest.mark.parametrize(
    "name,slot",
    [
        ("pi-sw1-p1", 0),
        ("pi-sw1-p10", 9),
        ("pi-sw2-p33", 48 + 32),
        ("pi-sw2-p08", 48 + 7),
        ("rpi5-netv2pcie-test", None),  # no match: falls back to the hash
    ],
)
def test_slot_sed_placement(tmp_path, name, slot):
    c = Client(tmp_path, BUSYBOX, config=PLACEMENT)
    c.arm()
    got = c.slot(name)
    if slot is None:
        assert 0 <= got < 96
    else:
        assert got == slot


@needs_busybox
def test_placement_gives_every_board_its_own_slot(tmp_path):
    # fpgas.online welland, 2026-09-15: 7 boards on sw1, 28 on sw2.
    c = Client(tmp_path, BUSYBOX, config=PLACEMENT)
    c.arm()
    sw1 = [10, 12, 14, 16, 17, 18, 38]
    sw2 = [3, 4, 5, 6, 7, 8, 16, *range(18, 25), 29, *range(33, 39), 40, 42, 43, 44, 46, 47, 48]
    names = [f"pi-sw1-p{p}" for p in sw1] + [f"pi-sw2-p{p}" for p in sw2]
    assert len({c.slot(n) for n in names}) == len(names)


# --- the reboot decision ---------------------------------------------------


@needs_busybox
def test_unchanged_root_never_reboots(client):
    for i in range(5):
        client.set_now(T0 + 60 * i)
        client.check()
    assert client.deadline() is None
    assert client.rebooted() == []


@needs_busybox
def test_generation_bump_warns_then_reboots_at_the_slot(client):
    gen = T0 + 1000
    client.set_now(gen + 30)
    client.write_lower(GEN, f"{gen} 2026-09-24T02:00:00Z 3 files changed\n")
    client.check()
    # base 420 + slot 33 * 20 s, measured from the generation's own timestamp
    # so every client shares the same reference point.
    expected = gen + 420 + 33 * 20
    assert client.deadline() == expected
    assert "generation changed" in client.kmsg.read_text()
    assert client.warned_at() is None

    # The warning goes out WARN_BEFORE plus one timer period ahead...
    client.set_now(expected - 300 - 61)
    client.check()
    assert client.warned_at() is None
    client.set_now(expected - 300 - 60)
    client.check()
    assert client.warned_at() == expected - 360

    client.set_now(expected - 1)
    client.check()
    assert client.rebooted() == []

    # ...so the reboot still lands on the deadline.
    client.set_now(expected)
    client.check()
    assert client.rebooted() == ["systemctl reboot"]
    assert "rebooting: generation changed" in client.kmsg.read_text()
    assert "REBOOTING NOW" in client.console.read_text()


@needs_busybox
def test_two_clients_never_share_a_reboot_window(tmp_path):
    gen = T0 + 1000
    deadlines = []
    for name in ("pi-sw2-p33", "pi-sw2-p34", "pi-sw1-p33"):
        c = Client(tmp_path / name, BUSYBOX, hostname=name, config=PLACEMENT)
        c.write_lower(GEN, "old\n")
        c.arm()
        c.set_now(gen + 90)  # each notices at a different time...
        c.write_lower(GEN, f"{gen} x\n")
        c.check()
        deadlines.append(c.deadline())
    # ...but all count from the generation, so slots stay SPACING apart.
    deadlines.sort()
    assert all(b - a >= 20 for a, b in zip(deadlines, deadlines[1:]))


@needs_busybox
def test_implausible_generation_time_falls_back_to_local_clock(client):
    client.set_now(T0 + 100)
    client.write_lower(GEN, "999999999999 x\n")
    client.check()
    assert client.deadline() == T0 + 100 + 420 + 33 * 20


@needs_busybox
def test_update_lock_holds_and_cancels(client):
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 + 50} x\n")
    client.check()
    assert client.deadline() is not None

    # The next update starts before this client's slot comes up.
    client.write_lower(LOCK, f"{T0 + 200} 2026-09-24T03:00:00Z update\n")
    client.set_now(T0 + 5000)
    out = client.check()
    assert "NFS root update in progress" in out
    assert client.deadline() is None
    assert client.rebooted() == []
    assert client.check() == ""  # the same hold is not logged again


@needs_busybox
def test_abandoned_update_lock_is_ignored_and_logged_once(client):
    client.write_lower(LOCK, f"{T0} x\n")
    client.write_lower(GEN, f"{T0 + 10} x\n")
    client.set_now(T0 + 86400 + 1)
    assert "ignoring update lock" in client.check()
    assert client.deadline() is not None
    assert "ignoring update lock" not in client.check()


@needs_busybox
def test_unparseable_lock_still_holds(client):
    client.write_lower(LOCK, "garbage\n")
    client.write_lower(GEN, f"{T0 + 10} x\n")
    client.set_now(T0 + 999999)
    assert "update in progress" in client.check()
    assert client.deadline() is None


@needs_busybox
def test_stale_lock_or_fleet_inhibit_still_holds(client):
    # A lock the NFS mount answers ESTALE for is not an absent lock.
    client.write_lower(LOCK, f"{T0} x\n")
    client.write_lower(GEN, f"{T0 + 10} x\n")
    client.set_now(T0 + 100)
    client.stale(str(client.lower / LOCK.lstrip("/")))
    assert "update in progress" in client.check()
    assert client.deadline() is None

    (client.lower / LOCK.lstrip("/")).unlink()
    client.stale(str(client.lower / FLEET_INHIBIT.lstrip("/")))
    assert "fleet inhibit" in client.check()


@needs_busybox
def test_unreadable_generation_is_not_a_change(client):
    marker = client.lower / GEN.lstrip("/")
    client.set_now(T0 + 7200)
    marker.chmod(0)
    try:
        if os.access(marker, os.R_OK):
            pytest.skip("running as root: cannot make the marker unreadable")
        out = client.check()
    finally:
        marker.chmod(0o644)
    assert "cannot read" in out
    assert client.deadline() is None


@needs_busybox
def test_fleet_and_local_inhibits_hold(client):
    client.write_lower(GEN, f"{T0 + 10} x\n")
    client.set_now(T0 + 10 + 420 + 33 * 20)  # already past this client's slot

    (client.lower / FLEET_INHIBIT.lstrip("/")).write_text("")
    assert "fleet inhibit" in client.check()
    (client.lower / FLEET_INHIBIT.lstrip("/")).unlink()

    client.inhibit.write_text("")
    assert "local inhibit" in client.check()
    client.inhibit.unlink()

    # Released: warned now, rebooted no sooner than WARN_BEFORE later.
    released = client.now
    assert client.run_until_reboot() == released + 300
    assert client.rebooted() == ["systemctl reboot"]


@needs_busybox
def test_stale_probe_needs_confirmation_and_a_quiet_root(client):
    client.set_now(T0 + 7200)
    client.age_lower_dirs(7200)
    client.stale("/var/lib/dpkg/status")

    client.check()
    assert client.deadline() is None  # one sighting is not enough

    client.set_now(T0 + 7260)
    client.check()
    # No generation to share, so the reference is local "now".
    assert client.deadline() == T0 + 7260 + 420 + 33 * 20
    assert "stale files for 2 checks: /var/lib/dpkg/status" in client.kmsg.read_text()


@needs_busybox
def test_stale_probe_while_root_is_still_changing_holds(client):
    # An update without nfsroot-generation: files go stale while dpkg is
    # still busy. Rebooting into that root is worse than waiting.
    client.set_now(T0 + 7200)
    client.age_lower_dirs(120)
    client.stale("/var/lib/dpkg/status")
    client.check()
    assert "changed 120s ago" in client.check()
    assert client.deadline() is None


@needs_busybox
def test_stale_count_resets_when_probes_recover(client):
    client.set_now(T0 + 7200)
    client.age_lower_dirs(7200)
    client.stale("/var/lib/dpkg/status")
    client.check()
    client.stale()
    client.check()
    client.stale("/var/lib/dpkg/status")
    client.check()
    assert client.deadline() is None


@needs_busybox
def test_vanished_probe_counts_as_stale(client):
    # Seen on real clients: a replaced file can surface as ENOENT.
    client.set_now(T0 + 7200)
    client.age_lower_dirs(7200)
    (client.root / "var/lib/dpkg/status").unlink()
    client.check()
    client.check()
    assert "/var/lib/dpkg/status(gone)" in client.kmsg.read_text()


@needs_busybox
def test_probe_missing_since_boot_is_not_stale(client):
    client.set_now(T0 + 7200)
    client.age_lower_dirs(7200)
    for _ in range(3):
        client.check()  # /etc/ld.so.cache never existed here
    assert client.deadline() is None


@needs_busybox
def test_logged_in_users_defer_the_reboot_up_to_the_grace(client):
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 + 100} x\n")
    client.check()
    deadline = client.deadline()
    (client.state / "fake-who").write_text("pi pts/0 2026-09-24 02:00 (10.0.0.1)\n")

    client.set_now(deadline - 360)
    client.check()  # warned, on pi's terminal too
    assert "will REBOOT" in (client.dev / "pts/0").read_text()

    client.set_now(deadline)
    assert "deferring reboot" in client.check()
    assert client.rebooted() == []

    client.set_now(deadline + 600)  # USER_GRACE is 600 s in these tests
    client.check()
    assert client.rebooted() == ["systemctl reboot"]


@needs_busybox
def test_stale_systemd_forces_the_reboot(client):
    # systemd-shutdown is what PID 1 execs at the very end of a clean
    # reboot; if it is stale PID 1 freezes, so do not even try.
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 - 5000} x\n")
    client.stale(str(client.systemd_shutdown))
    client.run_until_reboot()
    assert client.rebooted() == ["reboot -f"]
    assert "clean reboot unavailable" in client.kmsg.read_text()


@needs_busybox
def test_failed_systemctl_falls_back_to_forced_reboot(client):
    client._script(client.systemctl, f'echo "systemctl $*" >>"{client.calls}"; exit 1')
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 - 5000} x\n")
    client.run_until_reboot()
    assert client.rebooted() == ["systemctl reboot", "reboot -f"]


@needs_busybox
def test_dry_run_only_logs_once_and_writes_no_terminal(tmp_path):
    c = Client(tmp_path, BUSYBOX, config="DRY_RUN=1")
    c.write_lower(GEN, "old\n")
    c.arm()
    (c.state / "fake-who").write_text("pi pts/0 2026-09-24 02:00 (10.0.0.1)\n")
    c.write_lower(GEN, f"{T0 - 5000} x\n")
    for _ in range(20):  # past WARN_BEFORE and pi's USER_GRACE (600 s)
        c.check()
        c.set_now(c.now + 60)
    assert c.rebooted() == []
    assert c.kmsg.read_text().count("DRY_RUN: would reboot") == 1
    # The warning is still logged, but no terminal is told a reboot is coming.
    assert "will REBOOT" in c.kmsg.read_text()
    assert c.console.read_text() == ""
    assert not (c.dev / "pts/0").exists()


# --- the reboot warning ------------------------------------------------------


def _warn(client, who=""):
    """Schedule a reboot and advance to the moment the warning goes out."""
    if who:
        (client.state / "fake-who").write_text(who)
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 + 100} 2026-09-24T03:00:00Z 5 files changed\n")
    client.check()
    deadline = client.deadline()
    client.set_now(deadline - 360)
    out = client.check()
    assert client.warned_at() == deadline - 360
    return deadline, out


@needs_busybox
def test_warning_reaches_console_terminals_journal_and_kmsg(client):
    who = "pi pts/0 2026-09-24 02:00 (10.0.0.1)\nroot tty1 2026-09-24 01:00\npi pts/0 2026-09-24 02:05 (10.0.0.2)\n"
    deadline, out = _warn(client, who)
    hhmm = time.strftime("%H:%M:%S UTC", time.gmtime(deadline))
    for target in (client.console, client.dev / "pts/0", client.dev / "tty1"):
        text = target.read_text()
        assert "node33 will REBOOT in about 6 minutes, at " + hhmm in text, target
        assert "To STOP this reboot:  sudo nfsroot-watchdog inhibit" in text
        assert f"sudo touch {client.inhibit}" in text
        assert "undo with: sudo nfsroot-watchdog release" in text
        assert "5 files changed" in text
    # pts/0 appears twice in who but is written once.
    assert (client.dev / "pts/0").read_text().count("will REBOOT") == 1
    # The journal gets every line at warning priority, the kernel log too.
    lines = [l for l in out.splitlines() if l]
    assert lines and all(l.startswith("<4>") for l in lines)
    assert "To STOP this reboot" in client.kmsg.read_text()


@needs_busybox
def test_warning_is_sent_once(client):
    deadline, _ = _warn(client)
    client.set_now(deadline - 120)
    client.check()
    assert client.console.read_text().count("will REBOOT") == 1


@needs_busybox
def test_reboot_never_comes_sooner_than_warn_before_after_the_warning(client):
    # The clock jumps straight past the deadline (a suspended timer, a
    # clock step): the warning goes out now and the reboot waits for it.
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 + 100} x\n")
    client.check()
    late = client.deadline() + 3600
    client.set_now(late)
    client.check()
    assert client.warned_at() == late
    assert client.rebooted() == []
    client.set_now(late + 299)
    client.check()
    assert client.rebooted() == []
    client.set_now(late + 300)
    client.check()
    assert client.rebooted() == ["systemctl reboot"]


@needs_busybox
def test_late_notice_still_gets_the_full_warning(client):
    # A client that notices a generation long after its slot has passed.
    client.set_now(T0 + 100)
    client.write_lower(GEN, f"{T0 - 7200} x\n")
    client.check()
    assert client.deadline() == T0 + 100 + 300
    assert client.warned_at() == T0 + 100


@needs_busybox
def test_inhibit_after_the_warning_is_announced(client):
    deadline, _ = _warn(client, "pi pts/0 2026-09-24 02:00 (10.0.0.1)\n")
    client.inhibit.write_text("")
    client.set_now(deadline)
    client.check()
    assert client.rebooted() == []
    for target in (client.console, client.dev / "pts/0"):
        assert "is CANCELLED: local inhibit" in target.read_text()
    assert client.warned_at() is None
    # Released later (and pi has logged out): a fresh warning, and the full
    # WARN_BEFORE again.
    client.inhibit.unlink()
    (client.state / "fake-who").unlink()
    client.set_now(deadline + 60)
    assert client.run_until_reboot() == deadline + 60 + 300
    assert client.console.read_text().count("will REBOOT") == 2


@needs_busybox
def test_update_lock_after_the_warning_is_announced(client):
    deadline, _ = _warn(client)
    client.write_lower(LOCK, f"{deadline - 100} x update\n")
    client.set_now(deadline - 100)
    client.check()
    assert "CANCELLED: NFS root update in progress" in client.console.read_text()


@needs_busybox
def test_a_stuck_terminal_does_not_block_the_reboot(client):
    # Nobody reads this terminal: opening the FIFO for writing blocks.
    os.mkfifo(client.dev / "pts/9")
    t = time.monotonic()
    _warn(client, "pi pts/9 2026-09-24 02:00 (10.0.0.1)\n")
    assert time.monotonic() - t < 30
    assert "will REBOOT" in client.console.read_text()


@needs_busybox
def test_odd_tty_names_from_who_are_not_written(client):
    _warn(client, "x ../../etc/passwd 2026-09-24 02:00\ny /etc/shadow 2026-09-24 02:00\n")
    assert not (client.dev.parent / "etc").exists()


# --- the CLI ---------------------------------------------------------------


@needs_busybox
def test_cli_status_and_inhibit(client):
    env = {**client.env(), "PATH": str(Path(BUSYBOX).parent) + ":/usr/bin:/bin"}

    def cli(*args):
        r = subprocess.run([BUSYBOX, "sh", str(CLI), *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    out = cli("status")
    assert f"NFS root:          {client.lower}" in out
    assert "slot:              33" in out
    assert "booted generation:" in out
    cli("inhibit")
    assert client.inhibit.exists()
    assert "inhibited:" in cli("status")
    cli("release")
    assert not client.inhibit.exists()
    assert cli("slot", "node7").strip() == "7"


def test_protocol_paths_match_the_server():
    """Client defaults and nfsroot-generation must agree on the three paths."""
    defaults = (SRC / "defaults").read_text()
    server = (SRC / "nfsroot-generation").read_text()
    for var, path in (("GEN_FILE", GEN), ("LOCK_FILE", LOCK), ("FLEET_INHIBIT", FLEET_INHIBIT)):
        assert f"{var}={path}\n" in defaults
        assert f'{var} = "{path}"' in server


# --- the production arrangement ----------------------------------------------


@pytest.mark.skipif(
    not STATIC_BUSYBOX,
    reason="set NFSROOT_WATCHDOG_TEST_STATIC_BUSYBOX to a busybox-static binary",
)
def test_static_busybox_smoke(tmp_path):
    """Arm and check with Debian's static busybox and no fakes: every applet
    resolves inside the binary, and PATH points nowhere."""
    now = int(time.time())
    c = Client(tmp_path, STATIC_BUSYBOX, fakes=False, config="USER_GRACE=0\nBASE_DELAY=0\nSPACING=0\nWARN_BEFORE=0\nCHECK_INTERVAL=0")
    c.write_lower(GEN, "old\n")
    assert "generation 'old', slot 33" in c.arm()
    assert c.check() == ""
    assert c.rebooted() == []

    c.write_lower(GEN, f"{now - 100000} x\n")  # implausible: use local now
    c.check()
    assert c.rebooted() == ["systemctl reboot"]
    assert "will REBOOT" in c.console.read_text()
