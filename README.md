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

Once release `.deb` artifacts are published:

```bash
sudo apt install ./dexctl_<version>_all.deb
```

True `apt install dexctl` requires a maintained apt repository or PPA. That is a later distribution channel, not the default installation path.

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
3. let GitHub Actions build sdist / wheel / optional `.deb`
4. publish to GitHub Releases
5. publish to PyPI
6. update the Homebrew tap formula

## Packaging Notes

- Python package metadata lives in `pyproject.toml`
- CI and release workflows live in `.github/workflows/`
- Homebrew formula scaffolding lives in `packaging/homebrew/`
- Debian packaging scaffolding lives in `packaging/debian/`

## License

MIT
