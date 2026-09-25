#!/bin/sh
# Build the nfsroot-watchdog .debs inside a Debian container. Invoked by
# .github/workflows/deb.yml once per suite as:
#   docker run --rm -e SUITE=trixie -v "$PWD:/src" -w /src debian:trixie sh packaging/ci-build.sh
# and reproducible locally the same way. SUITE is the suite being built for
# (it sets the version's ~deb<R> suffix and the changelog's distribution);
# PR, when set, is the pull request number of a preview build. Writes the
# .debs to /src/built-debs.
set -eux

: "${SUITE:?SUITE must name the suite being built for}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential ca-certificates git dpkg-dev debhelper python3

# dpkg-buildpackage / git refuse to operate on a repo owned by another uid.
git config --global --add safe.directory /src

# Rolling release: derive the version from git and regenerate the changelog.
python3 packaging/deb-version.py --suite "$SUITE" ${PR:+--pr "$PR"} --write-changelog

# Binary-only build, unsigned (the apt repo is signed later, in the publish job).
dpkg-buildpackage -b -us -uc

mkdir -p built-debs
cp ../*.deb built-debs/
ls -l built-debs/
