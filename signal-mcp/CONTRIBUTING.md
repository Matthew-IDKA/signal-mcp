# Contributing to signal-mcp

Thanks for your interest in contributing!

## Setup

```bash
git clone https://github.com/YOUR_ORG/signal-mcp.git
cd signal-mcp
pip install -e ".[dev]"
```

## Running tests

```bash
pytest tests/ -v
```

Tests use [respx](https://lundberg.github.io/respx/) to mock HTTP calls to signal-cli-rest-api -- no running Signal instance is needed.

## Linting

```bash
ruff check .
```

Configuration is in `pyproject.toml`. CI enforces these checks on every PR.

## Submitting changes

1. Fork the repo and create a feature branch
2. Make your changes
3. Ensure tests pass and ruff is clean
4. Open a pull request with a clear description of what changed and why
