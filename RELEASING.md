# Releasing dexctl

## Preconditions

- `uv run python -m unittest` passes locally
- `README.md`, package metadata, and changelog are up to date
- Homebrew formula template still matches the release layout

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
   - build the `.deb` artifact when the Debian toolchain is available
   - attach artifacts to the GitHub Release
   - publish to PyPI

5. If `HOMEBREW_TAP_GITHUB_TOKEN` is configured, the workflow can also update the tap repository.

## Post-release verification

- `uv tool install dexctl==X.Y.Z`
- `pipx install dexctl==X.Y.Z`
- `brew install` from the tap
- `.deb` installation if the Debian artifact was produced
