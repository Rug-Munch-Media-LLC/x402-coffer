# vendor/x402 — vendored x402-py

This directory is a **vendored copy** of the x402 spec-conformance library so that
`rmi-mcp-x402`'s test suite (`tests/test_x402_spec.py`) and the in-repo domain
shim resolve to the *same* implementation the tests assert against, independent of
what `x402` happens to be installed in the ambient environment.

## Provenance
- Source: `/srv/work/repos/x402-py/x402/` (fleet reference checkout)
- Copied: 2026-08-21
- Files: `__init__.py batch.py facilitator.py handler.py middleware.py scheme.py
  self_facilitator.py solana_verifier.py`
- The source tree is a reference checkout without a resolvable `HEAD`, so this copy
  is pinned by date + source path rather than a git SHA. Re-copy from the same path
  when the reference is updated.

## How it is activated

A `vendor/` package on its own would only be importable as `vendor.x402`. To make the
plain top-level name `x402` resolve here, `coffer/_spec.py` loads `vendor/x402` directly
via `importlib` and registers it as the top-level `x402` module:

- The vendored path is resolved **relative to the package** (`coffer/_spec.py` →
  `../vendor/x402`), never a hardcoded absolute path.
- No `.pth` file and no `sys.path` manipulation is required — `import coffer` works in
  any venv, fresh or existing.
- `coffer._spec.SPEC_SOURCE` reports how the spec was resolved: `"facade-loaded"` (the
  normal, `.pth`-free path) or `"top-level"` (a pre-existing `.pth`/installed copy won).

Outside the venv (e.g. system `pytest`), `tests/conftest.py` independently resolves the
spec library from the same reference path, so both entry points agree.

> `[REVIEW]` This intentionally shadows any other `x402` distribution installed in the
> venv (e.g. the official Coinbase x402 SDK). `app/routers/*` import the official SDK's
> names inside `try/except ImportError` guards, so a shadowed/missing SDK degrades to
> `SDK_AVAILABLE = False` rather than crashing — but the routers will NOT exercise the
> official-SDK code paths while the vendored copy wins. This trade-off is mandated by the
> task (single vendored spec library) and is flagged for agent C (routers) to confirm.
