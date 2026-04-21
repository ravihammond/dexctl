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

4. Wait for the GitHub Actions `Release` workflow to:
   - run tests
   - build the sdist and wheel
   - render the Homebrew formula
   - build the `.deb` artifact
   - generate `Packages`, `Release`, `Release.gpg`, and `InRelease`
   - publish the signed apt repository to GitHub Pages
   - attach artifacts to the GitHub Release
   - publish to PyPI

5. If `HOMEBREW_TAP_GITHUB_TOKEN` is configured, the workflow can also update the tap repository.

## Post-release verification

- `uv tool install dexctl==X.Y.Z`
- `pipx install dexctl==X.Y.Z`
- `brew install` from the tap
- `curl -fsSL https://ravihammond.github.io/dexctl/apt/dexctl-archive-keyring.asc | gpg --show-keys`
- `sudo apt update && sudo apt install dexctl` on a clean Debian-family machine
- direct `.deb` installation from the GitHub Release as a fallback path
