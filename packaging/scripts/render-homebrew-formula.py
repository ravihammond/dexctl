#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the Homebrew formula from the checked-in template.")
    parser.add_argument("--version", required=True, help="Release version without the leading v.")
    parser.add_argument(
        "--artifact",
        required=True,
        help="Path to the release source tarball used by the formula.",
    )
    parser.add_argument(
        "--template",
        default="packaging/homebrew/dexctl.rb.in",
        help="Template path.",
    )
    parser.add_argument(
        "--output",
        default="packaging/homebrew/dexctl.rb",
        help="Output formula path.",
    )
    args = parser.parse_args()

    artifact = Path(args.artifact)
    sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    template = Path(args.template).read_text(encoding="utf-8")
    rendered = template.replace("@VERSION@", args.version).replace("@SHA256@", sha256)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
