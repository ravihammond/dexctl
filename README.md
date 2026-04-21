# dexctl

`dexctl` is a Python CLI for managing Codex account state, runtime auth installation, usage inspection, and safe account switching.

It is intended to be the single control plane for:

- active account selection
- runtime auth preparation and capture
- usage caching and refresh
- migration from legacy shell-managed state
- thin shell / terminal adapters that should not own account logic

## Status

`dexctl` is currently an alpha CLI with macOS and Linux support. The recommended install methods are isolated Python tool installs via `uv` or `pipx`.

## Install

### Option 1: `uv tool install`

Recommended if you already use `uv`.

```bash
uv tool install dexctl
```

To upgrade:

```bash
uv tool upgrade dexctl
```

To uninstall:

```bash
uv tool uninstall dexctl
```

### Option 2: `pipx install`

Recommended if you prefer the more established Python CLI installer.

```bash
pipx install dexctl
```

To upgrade:

```bash
pipx upgrade dexctl
```

To uninstall:

```bash
pipx uninstall dexctl
```

### Option 3: Homebrew tap

Once the tap is published:

```bash
brew tap ravihammond/dexctl
brew install dexctl
```

### Option 4: Debian package

Once the signed apt repository is published:

```bash
curl -fsSL https://ravihammond.github.io/dexctl/apt/dexctl-archive-keyring.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/dexctl-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/dexctl-archive-keyring.gpg] https://ravihammond.github.io/dexctl/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/dexctl.list >/dev/null
sudo apt update
sudo apt install dexctl
```

The hosted apt repository is intended for current Ubuntu LTS and Debian stable systems.

If you prefer a one-off manual install from a GitHub Release artifact, the fallback remains:

```bash
sudo apt install ./dexctl_<version>_all.deb
```

## Quick Start

Inspect the active account:

```bash
dexctl current
dexctl show
```

List all accounts:

```bash
dexctl ls
dexctl ls --cached
```

Switch interactively:

```bash
dexctl switch
dexctl ls --pick
```

Cycle to the next account:

```bash
dexctl cycle next
```

## Why managed installs matter

`dexctl` should be installed through a package manager that owns the executable environment. Do not create ad-hoc shell wrappers that import the repo directly from `src/` by mutating `PYTHONPATH` or `sys.path`.

Managed installs ensure declared dependencies such as `rich` and `prompt_toolkit` are installed correctly and keep the CLI behavior consistent across machines.

## Developer Setup

Clone the repo and sync the project environment:

```bash
uv sync
```

Run the CLI from the project environment:

```bash
uv run dexctl --help
uv run python -m unittest
```

Optional editable tool install for local development:

```bash
uv tool install --editable .
```

## Release Workflow

The intended release flow is:

1. run the full test suite locally
2. tag a release
3. let GitHub Actions build sdist / wheel / `.deb`
4. generate and sign the apt repository metadata
5. publish the apt repository to GitHub Pages
6. publish to GitHub Releases
7. publish to PyPI
8. update the Homebrew tap formula

## Packaging Notes

- Python package metadata lives in `pyproject.toml`
- CI and release workflows live in `.github/workflows/`
- Homebrew formula scaffolding lives in `packaging/homebrew/`
- Debian packaging scaffolding lives in `packaging/debian/`
- Apt repository generation lives in `packaging/scripts/build-apt-repo.py`

## License

MIT
