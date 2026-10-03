"""Tests for nfsroot-generation's one-directory-per-version commands
(publish, rollback, list, inhibit, uninhibit).

`publish` decides which version every client of the fleet runs: a marker
written in the wrong order, into the wrong entry, or with a stale time
reboots machines that should stay put, reboots a whole fleet at once, or
leaves machines on a version nobody serves any more. Everything runs
against a throwaway BASE under tmp_path.
"""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src/nfsroot-generation"

_loader = importlib.machinery.SourceFileLoader("nfsroot_generation", str(CLI))
_spec = importlib.util.spec_from_loader("nfsroot_generation", _loader)
ng = importlib.util.module_from_spec(_spec)
_loader.exec_module(ng)

D = "root/etc/nfsroot-watchdog"
OLD_MARKER = "1790910133 2026-10-02T03:02:13Z 8803 files changed"


def cli(*args, check=True):
    r = subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def cli_json(*args):
    return json.loads(cli(*args).stdout)


def refused(*args):
    r = cli(*args, check=False)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout == ""
    return r.stderr


def add_version(base, name):
    v = base / "versions" / name
    (v / "boot").mkdir(parents=True)
    (v / "root/etc").mkdir(parents=True)
    (v / "root/etc/passwd").write_text("pi:x:1000\n")
    return v


def add_legacy(base):
    """Today's in-place root, published by begin/end, as a symlink entry."""
    tree = base / "bookworm"
    (tree / "boot").mkdir(parents=True)
    (tree / D).mkdir(parents=True)
    (tree / D / "generation").write_text(OLD_MARKER + "\n")
    (base / "versions/legacy-bookworm").symlink_to("../bookworm")
    (base / "current").symlink_to("versions/legacy-bookworm")
    return tree


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "rpi"
    (b / "versions").mkdir(parents=True)
    return b


def marker(base, name):
    return (base / "versions" / name / D / "generation").read_text().strip()


def version_file(base, name):
    p = base / "versions" / name / D / "version"
    return p.read_text().strip() if p.exists() else None


# --- publish ---------------------------------------------------------------------


def test_first_publish(base):
    add_version(base, "v1")
    r = cli_json("publish", base, "v1")
    assert os.readlink(base / "current") == "versions/v1"  # relative: the export moves
    m = marker(base, "v1")
    epoch, _iso, name = m.split()
    assert name == "v1"
    assert abs(int(epoch) - time.time()) < 60
    assert version_file(base, "v1") == "v1"
    assert r == {"current": "v1", "previous": None, "marker": m,
                 "changed": [{"name": "v1", "wrote": ["marker", "version"]}]}
    assert (base / "versions/v1" / D).stat().st_mode & 0o777 == 0o755


def test_directory_is_readable_whatever_the_umask(base):
    add_version(base, "v1")
    r = subprocess.run(["sh", "-c", f'umask 077 && exec "{sys.executable}" "{CLI}" publish "{base}" v1'],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (base / "versions/v1" / D).stat().st_mode & 0o777 == 0o755
    assert (base / "versions/v1" / D / "generation").stat().st_mode & 0o777 == 0o644


def test_marker_lands_before_the_version_file(base, monkeypatch):
    # The legacy root's old-style marker ("... 8803 files changed") next to
    # a new version file would, for a moment, name the wrong version.
    add_legacy(base)
    add_version(base, "v1")
    order = []
    real = ng._write_atomic

    def spy(path, content):
        if "legacy" in path or "bookworm" in path:
            order.append(os.path.basename(path))
        real(path, content)

    monkeypatch.setattr(ng, "_write_atomic", spy)
    ng.make_current(str(base), "v1")
    assert order == ["generation", "version"]


def test_a_name_with_whitespace_is_refused(base):
    add_version(base, "a b")
    assert "one word" in refused("publish", base, "a b")


def test_publish_gives_every_published_version_the_new_marker(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    v1_version = base / "versions/v1" / D / "version"
    ino = v1_version.stat().st_ino
    add_version(base, "v2")
    r = cli_json("publish", base, "v2")
    assert os.readlink(base / "current") == "versions/v2"
    assert marker(base, "v1") == marker(base, "v2") == r["marker"]
    assert r["marker"].split()[2] == "v2"
    assert version_file(base, "v1") == "v1"
    assert v1_version.stat().st_ino == ino  # written once, never again
    assert r["previous"] == "v1"


def test_publish_order_new_version_first_then_swap_then_the_rest(base, monkeypatch):
    add_version(base, "v1")
    ng.make_current(str(base), "v1")
    old = marker(base, "v1")
    add_version(base, "v2")
    seen = {}
    real_swap = ng._swap_current

    def swap(b, name):
        # Before the swap: the new version is complete, the old untouched.
        seen["v2"] = (version_file(base, "v2"), marker(base, "v2"))
        seen["v1"] = marker(base, "v1")
        real_swap(b, name)
        seen["after"] = marker(base, "v1")

    monkeypatch.setattr(ng, "_swap_current", swap)
    r = ng.make_current(str(base), "v2")
    assert seen["v2"] == ("v2", r["marker"])
    assert seen["v1"] == old
    assert seen["after"] == old  # (iii) comes after the swap
    assert marker(base, "v1") == r["marker"]


def test_publish_migrates_the_legacy_root(base):
    tree = add_legacy(base)
    add_version(base, "20261005T013000Z-b41ea5c743cf")
    r = cli_json("publish", base, "20261005T013000Z-b41ea5c743cf")
    assert r["previous"] == "legacy-bookworm"
    assert os.readlink(base / "current") == "versions/20261005T013000Z-b41ea5c743cf"
    # The legacy tree gets its name and the marker, and nothing else.
    assert (tree / D / "version").read_text() == "legacy-bookworm\n"
    assert (tree / D / "generation").read_text() == r["marker"] + "\n"
    assert sorted(p.name for p in (tree / D).iterdir()) == ["generation", "version"]
    assert (base / "versions/legacy-bookworm").is_symlink()


def test_orphans_are_never_written(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "orphan")
    add_version(base, "v2")
    cli("inhibit", base)
    cli("publish", base, "v2")
    cli("uninhibit", base)
    assert not (base / "versions/orphan/root/etc/nfsroot-watchdog").exists()


def test_not_a_version_is_refused(base):
    (base / "versions/nobootdir/root").mkdir(parents=True)
    assert "needs boot/ and root/" in refused("publish", base, "nobootdir")
    assert "needs boot/ and root/" in refused("publish", base, "missing")
    assert "needs boot/ and root/" in refused("publish", base, "../versions/x")
    assert not (base / "current").exists()


def test_base_without_versions_is_an_error(tmp_path):
    r = cli("list", tmp_path, check=False)
    assert r.returncode == 2
    assert "has no versions/ directory" in r.stderr


def test_current_that_is_not_a_symlink_is_refused(base):
    add_version(base, "v1")
    (base / "current").mkdir()
    assert "is not a symlink" in refused("publish", base, "v1")
    assert not (base / "versions/v1" / D).exists()


def test_a_version_file_naming_another_version_is_refused(base):
    v = add_version(base, "v1")
    (v / D).mkdir()
    (v / D / "version").write_text("v9\n")
    assert "says it is version 'v9'" in refused("publish", base, "v1")


def test_an_update_lock_in_a_published_version_is_refused(base):
    tree = add_legacy(base)
    (tree / D / "update.lock").write_text("1790910133 x update\n")
    add_version(base, "v1")
    assert "update.lock exists in legacy-bookworm" in refused("publish", base, "v1")
    assert os.readlink(base / "current") == "versions/legacy-bookworm"
    assert not (base / "versions/v1" / D).exists()


def test_publishing_an_earlier_version_again_needs_rollback(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "v2")
    cli("publish", base, "v2")
    assert "use rollback" in refused("publish", base, "v1")


# --- publish is idempotent ---------------------------------------------------------


def test_publish_again_changes_nothing(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "v2")
    first = cli_json("publish", base, "v2")
    ino = (base / "versions/v1" / D / "generation").stat().st_ino
    again = cli_json("publish", base, "v2")
    assert again["changed"] == []
    assert again["marker"] == first["marker"]
    assert (base / "versions/v1" / D / "generation").stat().st_ino == ino


def test_publish_again_repairs_a_publish_that_died_after_the_swap(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    v1_marker = marker(base, "v1")
    add_version(base, "v2")
    first = cli_json("publish", base, "v2")
    (base / "versions/v1" / D / "generation").write_text(v1_marker + "\n")  # (iii) never ran
    again = cli_json("publish", base, "v2")
    assert again["marker"] == first["marker"]  # fresh: not stamped again
    assert again["changed"] == [{"name": "v1", "wrote": ["marker"]}]
    assert marker(base, "v1") == first["marker"]


def test_repair_long_after_the_swap_stamps_afresh(base):
    # Clients count their slots from the marker's epoch: an hour-old one
    # reaching them now would make every slot due at once.
    add_version(base, "v1")
    cli("publish", base, "v1")
    v1_marker = marker(base, "v1")
    add_version(base, "v2")
    cli("publish", base, "v2")
    old = f"{int(time.time()) - 3600} 2026-10-03T00:00:00Z v2"
    (base / "versions/v2" / D / "generation").write_text(old + "\n")
    (base / "versions/v1" / D / "generation").write_text(v1_marker + "\n")
    r = cli_json("publish", base, "v2")
    epoch, _iso, name = r["marker"].split()
    assert name == "v2"
    assert abs(int(epoch) - time.time()) < 60
    assert marker(base, "v1") == marker(base, "v2") == r["marker"]


def test_publish_again_after_dying_before_the_swap(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    v2 = add_version(base, "v2")
    (v2 / D).mkdir()
    (v2 / D / "version").write_text("v2\n")
    (v2 / D / "generation").write_text(f"{int(time.time()) - 3600} x v2\n")  # (i) only
    r = cli_json("publish", base, "v2")
    assert os.readlink(base / "current") == "versions/v2"
    assert abs(int(r["marker"].split()[0]) - time.time()) < 60  # stamped at the swap
    assert marker(base, "v1") == r["marker"]


# --- rollback ------------------------------------------------------------------------


def test_rollback_makes_an_earlier_version_current_with_a_fresh_marker(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "v2")
    published = cli_json("publish", base, "v2")
    time.sleep(1.1)
    r = cli_json("rollback", base, "v1")
    assert os.readlink(base / "current") == "versions/v1"
    assert r["marker"].split()[2] == "v1"
    assert int(r["marker"].split()[0]) > int(published["marker"].split()[0])
    assert marker(base, "v1") == marker(base, "v2") == r["marker"]
    assert [c["name"] for c in r["changed"]] == ["v1", "v2"]  # target first


def test_rollback_to_the_legacy_root_is_allowed(base):
    tree = add_legacy(base)
    add_version(base, "v1")
    cli("publish", base, "v1")
    r = cli_json("rollback", base, "legacy-bookworm")
    assert os.readlink(base / "current") == "versions/legacy-bookworm"
    assert (tree / D / "generation").read_text() == r["marker"] + "\n"
    assert r["marker"].split()[2] == "legacy-bookworm"


def test_rollback_to_a_version_never_published_is_refused(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "orphan")
    assert "never published: use publish" in refused("rollback", base, "orphan")


# --- inhibit -----------------------------------------------------------------------


def test_inhibit_holds_every_published_version_and_publish_carries_it(base):
    tree = add_legacy(base)
    add_version(base, "v1")
    cli("publish", base, "v1")
    r = cli_json("inhibit", base, "--reason", "canary on pi-sw1-p10")
    assert r == {"inhibited": True, "changed": ["legacy-bookworm", "v1"], "marker": None}
    assert (tree / D / "inhibit").read_text().strip().endswith(" canary on pi-sw1-p10")
    assert cli_json("inhibit", base)["changed"] == []  # already held

    add_version(base, "v2")
    cli("publish", base, "v2")
    assert (base / "versions/v2" / D / "inhibit").exists()

    current_ino = (base / "versions/v2" / D / "generation").stat().st_ino
    r = cli_json("uninhibit", base)
    assert r["inhibited"] is False
    assert r["changed"] == ["legacy-bookworm", "v1", "v2"]
    for name in ("legacy-bookworm", "v1", "v2"):
        assert not (base / "versions" / name / D / "inhibit").exists()
    # A fresh stamp for every version but the current one, whose clients
    # stay put and need no stagger.
    assert marker(base, "legacy-bookworm") == marker(base, "v1") == r["marker"]
    assert (base / "versions/v2" / D / "generation").stat().st_ino == current_ino
    assert cli_json("publish", base, "v2")["changed"] == []  # still complete


def test_uninhibit_restarts_the_stagger(base):
    # Held for longer than the stagger: counted from the publish, every
    # client's slot would be past and all would reboot together.
    add_version(base, "v1")
    cli("publish", base, "v1")
    cli("inhibit", base)
    add_version(base, "v2")
    cli("publish", base, "v2")
    held = f"{int(time.time()) - 7200} 2026-10-03T00:00:00Z v2"
    for name in ("v1", "v2"):
        (base / "versions" / name / D / "generation").write_text(held + "\n")
    r = cli_json("uninhibit", base)
    epoch, _iso, name = r["marker"].split()
    assert name == "v2"
    assert abs(int(epoch) - time.time()) < 60
    assert marker(base, "v1") == r["marker"]
    assert marker(base, "v2") == held  # the current version is not written


def test_uninhibit_restamps_before_it_releases(base, monkeypatch):
    # A client must never see the release next to the old epoch.
    add_version(base, "v1")
    ng.make_current(str(base), "v1")
    add_version(base, "v2")
    ng.make_current(str(base), "v2")
    ng.set_inhibit(str(base), True)
    order = []
    real_write, real_unlink = ng._write_atomic, os.unlink
    monkeypatch.setattr(ng, "_write_atomic", lambda p, c: (order.append("marker"), real_write(p, c)))
    monkeypatch.setattr(ng.os, "unlink", lambda p: (order.append("release"), real_unlink(p)))
    ng.set_inhibit(str(base), False)
    assert order == ["marker", "release", "release"]


def test_uninhibit_says_when_it_cannot_restart_the_stagger(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    cli("inhibit", base)
    (base / "empty/root").mkdir(parents=True)
    (base / "current").unlink()
    (base / "current").symlink_to("empty")  # by hand, against the rules
    r = cli("uninhibit", base)
    assert "warning: current names no published version" in r.stderr
    out = json.loads(r.stdout)
    assert out["changed"] == ["v1"] and out["marker"] is None and "warning" in out
    assert not (base / "versions/v1" / D / "inhibit").exists()


def test_uninhibit_with_nothing_held_changes_nothing(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    before = marker(base, "v1")
    assert cli_json("uninhibit", base) == {"inhibited": False, "changed": [], "marker": None}
    assert marker(base, "v1") == before


def test_rollback_carries_the_inhibit_state_of_the_current_version(base):
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "v2")
    cli("publish", base, "v2")
    # By hand, against the rules: v1 inhibited, v2 (current) not.
    (base / "versions/v1" / D / "inhibit").write_text("x\n")
    cli("rollback", base, "v1")
    assert not (base / "versions/v1" / D / "inhibit").exists()


def test_inhibit_with_nothing_published_is_refused(base):
    add_version(base, "orphan")
    assert "nothing to hold" in refused("inhibit", base)


# --- list ----------------------------------------------------------------------------


def test_list(base):
    add_legacy(base)
    add_version(base, "v1")
    cli("publish", base, "v1")
    add_version(base, "orphan")
    (base / "versions/stray-file").write_text("")
    r = cli_json("list", base)
    assert r["current"] == "v1"
    by = {v["name"]: v for v in r["versions"]}
    assert sorted(by) == ["legacy-bookworm", "orphan", "v1"]
    assert by["v1"]["current"] and by["v1"]["published"] and by["v1"]["version"] == "v1"
    assert by["legacy-bookworm"]["link"] == "../bookworm"
    assert by["legacy-bookworm"]["version"] == "legacy-bookworm"
    assert by["orphan"] == {"name": "orphan", "current": False, "published": False, "marker": None,
                            "version": None, "inhibited": False, "locked": False, "link": None}


def test_list_on_a_fresh_gateway(base):
    (base / "empty/root").mkdir(parents=True)
    (base / "current").symlink_to("empty")
    assert cli_json("list", base) == {"current": None, "versions": []}


# --- writes and locking ----------------------------------------------------------


def test_writes_never_go_through_a_shared_inode(base):
    # Versions may share identical files by hard link (rsync --link-dest).
    # If one ever shares a watchdog file, writing it in place would change
    # every version linked to it. Every write is a new file and a rename.
    add_version(base, "v1")
    cli("publish", base, "v1")
    m = base / "versions/v1" / D / "generation"
    shared = base / "shared-marker"
    os.link(m, shared)
    before = shared.read_text()
    add_version(base, "v2")
    cli("publish", base, "v2")
    cli("inhibit", base)
    cli("rollback", base, "v1")
    assert shared.read_text() == before
    assert shared.stat().st_ino != m.stat().st_ino


def test_commands_wait_for_each_other(base):
    add_version(base, "v1")
    fd = os.open(base / "nfsroot-generation.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert "has held" in refused("publish", base, "v1", "--wait", "1")
    finally:
        os.close(fd)
    assert not (base / "current").exists()
    cli("publish", base, "v1", "--wait", "1")
