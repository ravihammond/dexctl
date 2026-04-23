# Releasing dexctl

## Preconditions

- `uv run python -m unittest` passes locally
- `README.md`, package metadata, and changelog are up to date
- Homebrew formula template still matches the release layout
- GitHub Pages is enabled for this repository with the source set to GitHub Actions
- Repository secrets are configured for:
  - `APT_GPG_PRIVATE_KEY`
  - `APT_GPG_KEY_ID`
  - `APT_GPG_PASSPHRASE` if the signing key is passphrase protected
  - `HOMEBREW_TAP_GITHUB_TOKEN` if the tap should be updated automatically

## Release steps

1. Update `src/dexctl/__init__.py` and `pyproject.toml` version if needed.
2. Commit the release changes.
3. Create and push a tag:

   ```bash
   git tag vX.Y.Z
   git push origin main --tags
   ```

4. Optional dry run before the real release:

   ```bash
   git tag vX.Y.Z-test1
   git push origin vX.Y.Z-test1
   ```

   Tags that contain `-` still run the release workflow, but they skip PyPI publication and Homebrew tap updates so the apt repository and GitHub Release path can be exercised safely.

5. Wait for the GitHub Actions `Release` workflow to:
   - run tests
   - build the sdist and wheel
   - render the Homebrew formula
   - build the `.deb` artifact
   - generate `Packages`, `Release`, `Release.gpg`, and `InRelease`
   - publish the signed apt repository to GitHub Pages
   - attach artifacts to the GitHub Release
   - publish to PyPI for stable `vX.Y.Z` tags
   - update the Homebrew tap only for stable `vX.Y.Z` tags when `HOMEBREW_TAP_GITHUB_TOKEN` is configured

6. Confirm the published pages are reachable before telling users to install from apt. GitHub Pages propagation can take several minutes.

## Post-release verification

- `uv tool install dexctl==X.Y.Z`
- `pipx install dexctl==X.Y.Z`
- `brew install` from the tap
- `curl -fsSL https://ravihammond.github.io/dexctl/apt/dexctl-archive-keyring.asc | gpg --show-keys`
- `sudo apt update && sudo apt install dexctl` on a clean Debian-family machine
- direct `.deb` installation from the GitHub Release as a fallback path

## Troubleshooting

- If the apt repository files return `404`, wait a few minutes and retry after the `deploy-apt-repository` job succeeds.
- If `apt update` fails, verify the keyring path, the `deb` line, and that `InRelease` is reachable from the published Pages site.
