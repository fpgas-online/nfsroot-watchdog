"""Tests for nfsroot-generation, the server half (src/nfsroot-generation).

`end` decides whether to bump the generation marker, which makes every
nfsroot-watchdog client reboot itself. Bumping on an update that changed
nothing reboots a fleet for nothing; bumping or unlocking after a failed
update reboots it into a half-built root; not bumping after a real change
leaves clients on stale file handles. Everything runs against a throwaway
root under tmp_path.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src/nfsroot-generation"

_loader = importlib.machinery.SourceFileLoader("nfsroot_generation", str(CLI))
_spec = importlib.util.spec_from_loader("nfsroot_generation", _loader)
ng = importlib.util.module_from_spec(_spec)
_loader.exec_module(ng)

LOCK = ng.LOCK_FILE
GEN = ng.GEN_FILE


def make_root(base: Path) -> Path:
    root = base / "root"
    for d in ("etc", "var/lib/dpkg", "var/cache/apt", "tmp", "usr/bin", "home/pi/.ansible/tmp"):
        (root / d).mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text("Package: x\n")
    (root / "usr/bin/tool").write_text("old\n")
    (root / "etc/passwd").write_text("pi:x:1000\n")
    return root


def tick():
    # ctime has ns resolution but the clock tick can be coarser: make sure
    # anything written next is really newer than what came before.
    time.sleep(0.02)


def cli(*args, check=True):
    r = subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def cli_json(*args):
    return json.loads(cli(*args).stdout)


def lock_of(root):
    return root / LOCK.lstrip("/")


def gen_of(root):
    return root / GEN.lstrip("/")


# --- scan ------------------------------------------------------------------------


def test_nothing_changed(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    assert cli_json("scan", root) == {
        "bump": False,
        "count": 0,
        "reason": "nothing changed",
        "sample": [],
    }


def test_replaced_file_is_a_change(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    # dpkg's way: write status-new, rename over status (a new inode).
    new = root / "var/lib/dpkg/status-new"
    new.write_text("Package: x\nVersion: 2\n")
    new.replace(root / "var/lib/dpkg/status")
    r = cli_json("scan", root)
    assert r["bump"] is True
    assert r["sample"] == ["/var/lib/dpkg/status"]


def test_file_with_an_old_mtime_is_still_a_change(tmp_path):
    # dpkg unpacks with the package's own mtime; rsync -t and cp -p copy it.
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    f = root / "usr/bin/tool"
    f.write_text("new\n")
    os.utime(f, (1_000_000_000, 1_000_000_000))
    assert cli_json("scan", root)["sample"] == ["/usr/bin/tool"]


def test_default_ignores_and_directories_do_not_count(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    (root / "var/cache/apt/pkgcache.bin").write_text("x")
    (root / "tmp/x").write_text("x")
    (root / "home/pi/.ansible/tmp/x").write_text("x")  # glob: home/*/.ansible
    # a temp file created and removed: only the directory's ctime moves
    (root / "etc/.tmp").write_text("x")
    (root / "etc/.tmp").unlink()
    assert cli_json("scan", root)["bump"] is False


def test_extra_ignore(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    (root / "usr/bin/tool").write_text("new\n")
    assert cli_json("scan", root, "--ignore", "usr/bin")["bump"] is False


def test_symlink_counts_and_is_not_followed(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    (root / "usr/bin/link").symlink_to("/nonexistent/target")
    assert cli_json("scan", root)["sample"] == ["/usr/bin/link"]


def test_sample_is_capped(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    for i in range(30):
        (root / f"usr/bin/f{i}").write_text("x")
    r = cli_json("scan", root, "--sample", "5")
    assert r["count"] == 30
    assert len(r["sample"]) == 5


def test_no_lock_means_bump(tmp_path):
    root = make_root(tmp_path)
    r = cli_json("scan", root)
    assert r["bump"] is True
    assert r["count"] is None


# --- begin / end ---------------------------------------------------------------


def test_begin_keeps_an_existing_lock(tmp_path):
    root = make_root(tmp_path)
    first = cli_json("begin", root)
    assert first["taken"] is True
    tick()
    second = cli_json("begin", root)
    assert second["taken"] is False
    assert second["content"] == first["content"]


def test_noop_update_does_not_bump(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    r = cli_json("end", root)
    assert r["bumped"] is False
    assert not gen_of(root).exists()
    assert not lock_of(root).exists()


def test_real_change_bumps_then_unlocks(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    (root / "usr/bin/tool").write_text("new\n")
    r = cli_json("end", root)
    assert r["bumped"] is True
    marker = gen_of(root).read_text()
    assert marker == r["generation"] + "\n"
    epoch, _iso, *reason = marker.split()
    assert abs(int(epoch) - time.time()) < 60
    assert " ".join(reason) == "1 files changed"
    assert not lock_of(root).exists()


def test_marker_is_replaced_not_rewritten(tmp_path):
    # A new inode, so clients' cached handles to the old marker go stale.
    root = make_root(tmp_path)
    cli("begin", root)
    cli("end", root, "--bump", "always")
    ino = gen_of(root).stat().st_ino
    cli("begin", root)
    cli("end", root, "--bump", "always")
    assert gen_of(root).stat().st_ino != ino


def test_bump_never_and_always(tmp_path):
    root = make_root(tmp_path)
    cli("begin", root)
    tick()
    (root / "usr/bin/tool").write_text("new\n")
    assert cli_json("end", root, "--bump", "never")["bumped"] is False
    assert not gen_of(root).exists()
    cli("begin", root)
    assert cli_json("end", root, "--bump", "always")["bumped"] is True


def test_status(tmp_path):
    root = make_root(tmp_path)
    assert cli_json("status", root) == {"lock": None, "generation": None, "fleet_inhibit": None}
    cli("begin", root, "--label", "apt upgrade")
    assert cli_json("status", root)["lock"].endswith(" apt upgrade")


def test_custom_paths(tmp_path):
    root = make_root(tmp_path)
    cli("--lock", "/var/lib/mylock", "begin", root)
    assert (root / "var/lib/mylock").exists()
    tick()
    (root / "usr/bin/tool").write_text("new\n")
    cli("--lock", "/var/lib/mylock", "--generation", "/etc/gen", "end", root)
    assert (root / "etc/gen").exists()
    assert not (root / "var/lib/mylock").exists()


# --- run ---------------------------------------------------------------------------


def test_run_success_publishes(tmp_path):
    root = make_root(tmp_path)
    r = cli_json("run", root, "--", "sh", "-c", f"echo new > {root}/usr/bin/tool")
    assert r["bumped"] is True
    assert not lock_of(root).exists()


def test_run_failure_keeps_the_lock_and_the_next_run_publishes_both(tmp_path):
    root = make_root(tmp_path)
    r = cli("run", root, "--", "sh", "-c", f"echo new > {root}/usr/bin/tool; exit 3", check=False)
    assert r.returncode == 3
    assert "leaving the update lock" in r.stderr
    assert lock_of(root).exists()
    assert not gen_of(root).exists()

    # The retry changes nothing itself, but the kept lock still dates from
    # the failed attempt, so its change is published now.
    r = cli_json("run", root, "--", "true")
    assert r["bumped"] is True
    assert r["sample"] == ["/usr/bin/tool"]


def test_run_needs_a_command(tmp_path):
    root = make_root(tmp_path)
    r = cli("run", root, check=False)
    assert r.returncode != 0
    assert "needs a command" in r.stderr


def test_root_must_exist(tmp_path):
    r = cli("begin", tmp_path / "missing", check=False)
    assert r.returncode != 0
