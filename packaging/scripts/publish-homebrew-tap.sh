#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <tap-repo-url> <formula-path>" >&2
  exit 1
fi

TAP_REPO_URL="$1"
FORMULA_PATH="$2"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

git clone "$TAP_REPO_URL" "$WORKDIR/tap"
mkdir -p "$WORKDIR/tap/Formula"
cp "$FORMULA_PATH" "$WORKDIR/tap/Formula/dexctl.rb"

(
  cd "$WORKDIR/tap"
  if git diff --quiet -- Formula/dexctl.rb; then
    echo "Homebrew formula unchanged."
    exit 0
  fi
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add Formula/dexctl.rb
  git commit -m "Update dexctl formula"
  git push
)
