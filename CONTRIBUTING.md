# Contributing

Thanks for helping. This is a community client for the Selcom API gateway. It follows the shared rules in [selcom-spec](https://github.com/selcom-developers/selcom-spec); if the client and the specification disagree, change the specification first.

## Setup

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"       # Windows: .venv\Scripts\pip
```

## Before you open a pull request

```
ruff check . && ruff format --check .
mypy
pytest
```

- Keep `mypy --strict` clean and the public API fully typed.
- Add a test with every change. Tests never touch the network; use `httpx.MockTransport`.
- Write the code yourself. Do not copy code from other Selcom clients.
- Use made-up test data only: phones `2557000000xx`, IDs like `ORD-1001`, keys like `test-key-0001`. Never commit real credentials, customer data or details of anyone's integration.
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Describe fixes neutrally in commits and pull requests ("Tighten error classification"), and report security problems privately (see `SECURITY.md`).

## Vectors

`tests/vectors` is a byte-for-byte copy of the specification's vectors at the tag in `SPEC_VERSION`. Never edit it by hand. To move to a newer specification, update `SPEC_VERSION` and run `python scripts/refresh_vectors.py`.
