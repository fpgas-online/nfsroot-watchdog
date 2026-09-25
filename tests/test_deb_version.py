"""Tests for packaging/deb-version.py's suite and preview suffixes.

The version rules are mithro/apt-repo-action's docs/packaging.md
("Versions"): every suite but sid carries `~deb<R>`, so the older suite's
build of a commit sorts lower, and a pull request preview carries `~pr<P>`
last, so it never upgrades over the default branch's build.
"""

import importlib.machinery
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packaging/deb-version.py"

_loader = importlib.machinery.SourceFileLoader("deb_version", str(SCRIPT))
_spec = importlib.util.spec_from_loader("deb_version", _loader)
dv = importlib.util.module_from_spec(_spec)
_loader.exec_module(dv)


@pytest.mark.parametrize("suite, pr, want", [
    ("bookworm", None, "0.3.post134~deb12"),
    ("trixie", None, "0.3.post134~deb13"),
    ("forky", None, "0.3.post134~deb14"),
    ("sid", None, "0.3.post134"),
    ("raspbian-trixie", None, "0.3.post134~deb13"),
    ("trixie", 41, "0.3.post134~deb13~pr41"),
    ("sid", 41, "0.3.post134~pr41"),
])
def test_suffixes(suite, pr, want):
    assert dv.with_suffixes("0.3.post134", suite, pr) == want


def test_unknown_suite_is_an_error():
    # A suite with no known release number would silently get no suffix and
    # sort above every other suite's build.
    with pytest.raises(SystemExit):
        dv.with_suffixes("0.3", "buster", None)


# docs/packaging.md's example, lowest first.
ORDER = [
    "0.3.post134~deb12",
    "0.3.post134~deb13~pr41",
    "0.3.post134~deb13",
    "0.3.post134~deb14",
    "0.3.post134",
    "0.3.post135~deb12",
]


@pytest.mark.skipif(not shutil.which("dpkg"), reason="needs dpkg --compare-versions")
@pytest.mark.parametrize("lower, higher", list(zip(ORDER, ORDER[1:])))
def test_order(lower, higher):
    subprocess.run(["dpkg", "--compare-versions", lower, "lt", higher], check=True)


def test_changelog_entry():
    entry = dv.changelog_entry("0.1.post3~deb13", "trixie", "0123abc", "Thu, 24 Sep 2026 12:00:00 +0000")
    assert entry == (
        "nfsroot-watchdog (0.1.post3~deb13) trixie; urgency=medium\n"
        "\n"
        "  * Built from fpgas-online/nfsroot-watchdog@0123abc\n"
        "\n"
        " -- Tim 'mithro' Ansell <me@mith.ro>  Thu, 24 Sep 2026 12:00:00 +0000\n"
    )
