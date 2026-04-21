#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="$ROOT/packaging/out"
mkdir -p "$OUT_DIR"

if ! command -v dpkg-buildpackage >/dev/null 2>&1; then
  echo "dpkg-buildpackage is required to build Debian artifacts." >&2
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

PKGROOT="$TMPDIR/dexctl"
mkdir -p "$PKGROOT"
cp -R "$ROOT"/. "$PKGROOT"/
rm -rf "$PKGROOT/.git" "$PKGROOT/.venv" "$PKGROOT/packaging/out"
cp -R "$ROOT/packaging/debian" "$PKGROOT/debian"

(
  cd "$PKGROOT"
  dpkg-buildpackage -us -uc -b
)

find "$TMPDIR" -maxdepth 1 -name '*.deb' -exec cp {} "$OUT_DIR"/ \;
