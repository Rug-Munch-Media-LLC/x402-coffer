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
plain top-level name `x402` resolve here, the worktree venv ships an import-line `.pth`:

    .venv/lib/python3.12/site-packages/_x402_vendor.pth
        import sys; sys.path.insert(0, "/tmp/opencode/x402-wt-deps/vendor")

The `.pth` import line runs at interpreter startup and places `vendor/` at the front of
`sys.path`, so `import x402` / `from x402 import build_payment_required, FacilitatorRouter`
bind to this vendored copy inside the venv. Outside the venv (e.g. system `pytest`),
`tests/conftest.py` independently resolves the spec library from the same reference
path, so both entry points agree.

> `[REVIEW]` This intentionally shadows any other `x402` distribution installed in the
> venv (e.g. the official Coinbase x402 SDK). `app/routers/*` import the official SDK's
> names inside `try/except ImportError` guards, so a shadowed/missing SDK degrades to
> `SDK_AVAILABLE = False` rather than crashing — but the routers will NOT exercise the
> official-SDK code paths while the vendored copy wins. This trade-off is mandated by the
> task (single vendored spec library) and is flagged for agent C (routers) to confirm.
