from __future__ import annotations

import glob
import gzip
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import build_legacy_home, migrate_app


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_cli_import_does_not_require_prompt_toolkit_for_noninteractive_paths(self) -> None:
        code = """
import builtins
import pathlib
import sys

repo = pathlib.Path.cwd() / "src"
sys.path.insert(0, str(repo))
real_import = builtins.__import__

def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.startswith("prompt_toolkit"):
        raise ModuleNotFoundError(name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = blocked_import
import dexctl.cli
print("ok")
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_built_wheel_installs_and_runs_show(self) -> None:
        subprocess.run(
            ["uv", "build"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        wheels = sorted(glob.glob(str(REPO_ROOT / "dist" / "*.whl")))
        self.assertTrue(wheels, "expected uv build to produce a wheel")
        wheel = wheels[-1]

        home = build_legacy_home()
        migrate_app(home.app)

        with tempfile.TemporaryDirectory(prefix="dexctl-wheel-venv-") as tmpdir:
            venv_dir = pathlib.Path(tmpdir)
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
            bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            python = bin_dir / "python"
            dexctl = bin_dir / "dexctl"
            subprocess.run([str(python), "-m", "pip", "install", wheel], check=True)

            help_proc = subprocess.run(
                [str(dexctl), "--help"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("dexctl", help_proc.stdout)

            env = os.environ.copy()
            env["HOME"] = str(home.root)
            env["DEXCTL_ROOT"] = str(home.root / ".codex-account")
            show_proc = subprocess.run(
                [str(dexctl), "show"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("alice@example.com", show_proc.stdout)
            self.assertNotIn("ModuleNotFoundError", show_proc.stderr)

    def test_build_apt_repo_generates_packages_and_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dexctl-apt-repo-") as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            deb_path = tmp_path / "dexctl_0.1.0_all.deb"
            deb_path.write_bytes(b"fake deb payload for repository metadata tests")
            output_dir = tmp_path / "site" / "apt"

            subprocess.run(
                [
                    sys.executable,
                    "packaging/scripts/build-apt-repo.py",
                    "--deb",
                    str(deb_path),
                    "--output-dir",
                    str(output_dir),
                    "--package-name",
                    "dexctl",
                    "--package-version",
                    "0.1.0",
                    "--architecture",
                    "all",
                    "--maintainer",
                    "Ravi Hammond",
                    "--description",
                    "Codex account control plane",
                    "--depends",
                    "python3:any",
                ],
                cwd=REPO_ROOT,
                check=True,
            )

            packages_path = output_dir / "dists" / "stable" / "main" / "binary-all" / "Packages"
            packages_gz_path = output_dir / "dists" / "stable" / "main" / "binary-all" / "Packages.gz"
            release_path = output_dir / "dists" / "stable" / "Release"
            pool_deb = output_dir / "pool" / "main" / "d" / "dexctl" / deb_path.name

            self.assertTrue(pool_deb.is_file())
            self.assertTrue(packages_path.is_file())
            self.assertTrue(packages_gz_path.is_file())
            self.assertTrue(release_path.is_file())

            packages_text = packages_path.read_text(encoding="utf-8")
            self.assertIn("Package: dexctl", packages_text)
            self.assertIn("Version: 0.1.0", packages_text)
            self.assertIn("Filename: pool/main/d/dexctl/dexctl_0.1.0_all.deb", packages_text)
            self.assertIn("Depends: python3:any", packages_text)

            with gzip.open(packages_gz_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), packages_text)

            release_text = release_path.read_text(encoding="utf-8")
            self.assertIn("Suite: stable", release_text)
            self.assertIn("Architectures: all", release_text)
            self.assertIn("Components: main", release_text)
            self.assertIn("main/binary-all/Packages", release_text)
            self.assertIn("main/binary-all/Packages.gz", release_text)
