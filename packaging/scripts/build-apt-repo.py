#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


def parse_control_paragraph(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    current_value: list[str] = []

    for raw_line in text.splitlines():
        if not raw_line:
            continue
        if raw_line[0].isspace():
            if current_key is None:
                raise ValueError("Malformed control paragraph continuation line.")
            current_value.append(raw_line[1:])
            continue
        if current_key is not None:
            fields[current_key] = "\n".join(current_value)
        key, value = raw_line.split(":", 1)
        current_key = key
        current_value = [value.lstrip()]

    if current_key is not None:
        fields[current_key] = "\n".join(current_value)

    return fields


def format_control_paragraph(fields: dict[str, str], field_order: Iterable[str] | None = None) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    keys = list(field_order or [])
    keys.extend(key for key in fields if key not in keys)

    for key in keys:
        value = fields.get(key)
        if value is None:
            continue
        seen.add(key)
        chunks = value.split("\n")
        lines.append(f"{key}: {chunks[0]}")
        lines.extend(f" {chunk}" if chunk else " ." for chunk in chunks[1:])

    for key, value in fields.items():
        if key in seen:
            continue
        chunks = value.split("\n")
        lines.append(f"{key}: {chunks[0]}")
        lines.extend(f" {chunk}" if chunk else " ." for chunk in chunks[1:])

    return "\n".join(lines) + "\n"


def sha_file(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_deb_metadata(path: Path) -> dict[str, str]:
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        return {}

    try:
        proc = subprocess.run(
            [dpkg_deb, "-f", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return {}
    return parse_control_paragraph(proc.stdout)


def sign_release_file(release_path: Path, key_id: str, passphrase_env: str | None) -> None:
    passphrase = os.environ.get(passphrase_env) if passphrase_env else None
    base_cmd = ["gpg", "--batch", "--yes", "--local-user", key_id]
    if passphrase is not None:
        base_cmd.extend(["--pinentry-mode", "loopback", "--passphrase", passphrase])

    subprocess.run(
        [
            *base_cmd,
            "--armor",
            "--detach-sign",
            "--output",
            str(release_path.with_name("Release.gpg")),
            str(release_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            *base_cmd,
            "--clearsign",
            "--output",
            str(release_path.with_name("InRelease")),
            str(release_path),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a signed apt repository tree from a release .deb.")
    parser.add_argument("--deb", required=True, help="Path to the .deb artifact.")
    parser.add_argument("--output-dir", required=True, help="Destination directory for the apt repository.")
    parser.add_argument("--package-name", help="Override package name.")
    parser.add_argument("--package-version", help="Override package version.")
    parser.add_argument("--architecture", help="Override package architecture.")
    parser.add_argument("--maintainer", help="Override package maintainer.")
    parser.add_argument("--description", help="Override package description.")
    parser.add_argument("--depends", help="Override package dependency string.")
    parser.add_argument("--section", default="utils", help="Debian package section.")
    parser.add_argument("--priority", default="optional", help="Debian package priority.")
    parser.add_argument("--suite", default="stable", help="Repository suite/codename.")
    parser.add_argument("--component", default="main", help="Repository component.")
    parser.add_argument("--origin", default="dexctl", help="Release metadata Origin field.")
    parser.add_argument("--label", default="dexctl", help="Release metadata Label field.")
    parser.add_argument(
        "--description-label",
        default="dexctl apt repository",
        help="Release metadata Description field.",
    )
    parser.add_argument("--gpg-key-id", help="GPG key ID or fingerprint used to sign Release metadata.")
    parser.add_argument(
        "--gpg-passphrase-env",
        help="Environment variable containing the GPG passphrase, if the key requires one.",
    )
    args = parser.parse_args()

    deb_path = Path(args.deb).resolve()
    if not deb_path.is_file():
        raise FileNotFoundError(f"Missing .deb artifact: {deb_path}")

    metadata = load_deb_metadata(deb_path)
    field_overrides = {
        "Package": args.package_name,
        "Version": args.package_version,
        "Architecture": args.architecture,
        "Maintainer": args.maintainer,
        "Description": args.description,
        "Depends": args.depends,
        "Section": args.section,
        "Priority": args.priority,
    }
    metadata.update({key: value for key, value in field_overrides.items() if value is not None})

    required_fields = ["Package", "Version", "Architecture", "Maintainer", "Description"]
    missing = [field for field in required_fields if not metadata.get(field)]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required package metadata for apt repository generation: {joined}")

    output_dir = Path(args.output_dir).resolve()
    pool_dir = output_dir / "pool" / args.component / metadata["Package"][0].lower() / metadata["Package"]
    packages_dir = output_dir / "dists" / args.suite / args.component / f"binary-{metadata['Architecture']}"
    output_dir.mkdir(parents=True, exist_ok=True)
    pool_dir.mkdir(parents=True, exist_ok=True)
    packages_dir.mkdir(parents=True, exist_ok=True)

    pool_deb = pool_dir / deb_path.name
    shutil.copy2(deb_path, pool_deb)

    packages_entry = {
        "Package": metadata["Package"],
        "Version": metadata["Version"],
        "Architecture": metadata["Architecture"],
        "Maintainer": metadata["Maintainer"],
        "Filename": pool_deb.relative_to(output_dir).as_posix(),
        "Size": str(pool_deb.stat().st_size),
        "MD5sum": sha_file(pool_deb, "md5"),
        "SHA1": sha_file(pool_deb, "sha1"),
        "SHA256": sha_file(pool_deb, "sha256"),
        "Section": metadata.get("Section", args.section),
        "Priority": metadata.get("Priority", args.priority),
        "Description": metadata["Description"],
    }
    for key in ("Depends", "Homepage"):
        if metadata.get(key):
            packages_entry[key] = metadata[key]

    packages_path = packages_dir / "Packages"
    packages_text = format_control_paragraph(
        packages_entry,
        field_order=[
            "Package",
            "Version",
            "Architecture",
            "Maintainer",
            "Depends",
            "Homepage",
            "Filename",
            "Size",
            "MD5sum",
            "SHA1",
            "SHA256",
            "Section",
            "Priority",
            "Description",
        ],
    )
    packages_path.write_text(packages_text, encoding="utf-8")

    packages_gz_path = packages_dir / "Packages.gz"
    with packages_gz_path.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw_handle, mtime=0) as handle:
            handle.write(packages_text.encode("utf-8"))

    release_path = output_dir / "dists" / args.suite / "Release"
    date_value = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    release_lines = [
        f"Origin: {args.origin}",
        f"Label: {args.label}",
        f"Suite: {args.suite}",
        f"Codename: {args.suite}",
        f"Date: {date_value}",
        f"Architectures: {metadata['Architecture']}",
        f"Components: {args.component}",
        f"Description: {args.description_label}",
    ]

    release_files = [packages_path, packages_gz_path]
    relative_release_files = [path.relative_to(release_path.parent).as_posix() for path in release_files]
    for algo, label in (("md5", "MD5Sum"), ("sha1", "SHA1"), ("sha256", "SHA256")):
        release_lines.append(f"{label}:")
        for relative_path, path in zip(relative_release_files, release_files, strict=True):
            digest = sha_file(path, algo)
            size = path.stat().st_size
            release_lines.append(f" {digest} {size:16d} {relative_path}")

    release_path.write_text("\n".join(release_lines) + "\n", encoding="utf-8")

    if args.gpg_key_id:
        sign_release_file(release_path, args.gpg_key_id, args.gpg_passphrase_env)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
