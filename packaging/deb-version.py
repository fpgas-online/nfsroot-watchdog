#!/usr/bin/env python3
"""Derive the Debian package version from ``git describe``.

At tag ``vX.Y`` the version is ``X.Y``; N commits later ``X.Y.postN``. With no
matching tag (the current upstream state) it falls back to ``0.0.post<commit
count>``. It increments on every commit, so each push publishes a new,
upgradeable package with no manual bump and no tag. All forms are valid Debian
versions verbatim, and match the hatch-vcs ``post-release`` scheme used for the
PyPI wheel, so the .deb and the wheel carry the same version.

``--suite`` adds ``~deb<R>`` for every suite but sid (``R`` is the Debian
release number), so the older suite's build of a commit sorts lower and
``apt full-upgrade`` across a release replaces it. ``--pr`` adds ``~pr<P>``
last, so a pull request preview never upgrades over the build from main.
These are mithro/apt-repo-action's docs/packaging.md rules ("Versions").

Usage:
    python3 packaging/deb-version.py --suite trixie        # print the version
    python3 packaging/deb-version.py --suite trixie [--pr 6] --write-changelog
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# REPO normally points at this script's own checkout, but can be overridden
# (e.g. by tests) to point ``git`` at an arbitrary repo instead.
_DEFAULT_REPO = Path(__file__).resolve().parent.parent
_REPO_OVERRIDE = os.environ.get("DEB_VERSION_REPO")
REPO = Path(_REPO_OVERRIDE) if _REPO_OVERRIDE else _DEFAULT_REPO
CHANGELOG = REPO / "debian" / "changelog"
SOURCE = "nfsroot-watchdog"
GITHUB_REPOSITORY = "fpgas-online/nfsroot-watchdog"
MAINTAINER = "Tim 'mithro' Ansell <me@mith.ro>"
# Debian codename -> release number, for the ~deb<R> suffix. sid has none.
DEBIAN_RELEASE = {"bookworm": 12, "trixie": 13, "forky": 14}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fail(message: str) -> None:
    """Print a clear error to stderr and exit non-zero (no traceback noise)."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _is_shallow_repo() -> bool:
    """Best-effort shallow-clone detection.

    Returns False (i.e. "assume non-shallow") if the git binary is missing or
    too old to support ``--is-shallow-repository`` -- those situations are
    handled explicitly by the callers that actually need git to work.
    """
    try:
        return _git("rev-parse", "--is-shallow-repository") == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def version() -> str:
    """git-describe derived version: vX.Y -> X.Y, N commits later -> X.Y.postN."""
    if _is_shallow_repo():
        _fail(
            "this is a shallow git clone (e.g. `git clone --depth 1`, or a CI "
            "checkout without full history). packaging/deb-version.py derives "
            "the package version from the full commit history (git describe / "
            "`git rev-list --count`), and in a shallow clone that count is "
            "truncated -- it would silently produce a wrong version (e.g. "
            "0.0.post1) that looks like a downgrade from previously published "
            "releases, instead of failing. Fix: use a full clone, e.g. `git "
            "fetch --unshallow`, or set `fetch-depth: 0` on the checkout step "
            "in the build workflow."
        )
    try:
        describe = _git("describe", "--tags", "--long", "--match", "v[0-9]*")
        m = re.match(r"^v(.+)-(\d+)-g[0-9a-f]+$", describe)
        if m:
            base, n = m.group(1), int(m.group(2))
            return base if n == 0 else f"{base}.post{n}"
    except subprocess.CalledProcessError:
        pass
    except FileNotFoundError:
        _fail(
            "'git' executable not found on PATH; packaging/deb-version.py "
            "cannot derive a package version without it. Install git (and "
            "ensure it is on PATH) before running this script or the build."
        )
    # No matching tag yet: fall back to a monotonic count-based version.
    try:
        return "0.0.post" + _git("rev-list", "--count", "HEAD")
    except Exception:
        return "0.0"


def with_suffixes(base: str, suite: str, pr: int | None) -> str:
    """Add ~deb<R> (every suite but sid) and then ~pr<P> (previews) to base."""
    codename = suite.removeprefix("raspbian-")
    if codename == "sid":
        out = base
    elif codename in DEBIAN_RELEASE:
        out = f"{base}~deb{DEBIAN_RELEASE[codename]}"
    else:
        _fail(f"unknown suite {suite!r}: no Debian release number for its ~deb<R> suffix "
              f"(known: {', '.join([*DEBIAN_RELEASE, 'sid'])})")
    return f"{out}~pr{pr}" if pr else out


def changelog_entry(ver: str, suite: str, sha: str, date: str) -> str:
    """The one changelog entry a build carries (docs/packaging.md, "The changelog")."""
    return (
        f"{SOURCE} ({ver}) {suite}; urgency=medium\n\n"
        f"  * Built from {GITHUB_REPOSITORY}@{sha}\n\n"
        f" -- {MAINTAINER}  {date}\n"
    )


def write_changelog(ver: str, suite: str) -> None:
    # The committer time, not the build time: dpkg-buildpackage takes
    # SOURCE_DATE_EPOCH from this entry, so a rebuild is reproducible.
    sha = _git("rev-parse", "HEAD")
    date = _git("log", "-1", "--format=%cd", "--date=rfc2822")
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    CHANGELOG.write_text(changelog_entry(ver, suite, sha, date))


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive the package version from git")
    ap.add_argument("--suite", required=True,
                    help="the suite being built for: bookworm, trixie, forky, sid (or raspbian-<codename>)")
    ap.add_argument("--pr", type=int, default=None,
                    help="pull request number, for a preview build")
    ap.add_argument("--write-changelog", action="store_true",
                    help="regenerate debian/changelog for the git-derived version")
    args = ap.parse_args()
    ver = with_suffixes(version(), args.suite, args.pr)
    if args.write_changelog:
        write_changelog(ver, args.suite)
    else:
        print(ver)


if __name__ == "__main__":
    main()
