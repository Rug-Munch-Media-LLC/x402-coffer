"""Resolve the vendored x402 spec implementation for the Coffer facade.

The vendored spec (``vendor/x402/``) is written as a top-level ``x402``
package — its submodules do ``from x402 import ...`` internally. In Coffer
runtimes a venv ``.pth`` places ``vendor/`` at the front of ``sys.path`` so
``import x402`` binds to the vendored copy (see ``vendor/README.md``). Where
that activation is absent — e.g. an environment where the official Coinbase
``x402`` SDK (v2.x) is installed — this module loads ``vendor/x402``
explicitly and registers it as the top-level ``x402`` module so the vendored
package's internal imports resolve to itself.

[REVIEW] Shadowing trade-off: when the resolvable top-level ``x402`` is
NOT the vendored spec, registration replaces ``sys.modules["x402"]``
process-wide. This is the same trade-off the venv ``.pth`` makes at
interpreter startup and is mandated by the single-vendored-spec-library
design: ``app/routers/*`` import the official SDK inside ``try/except
ImportError`` guards and degrade to ``SDK_AVAILABLE = False`` when the
vendored copy wins. Code that truly needs the official Coinbase SDK surface
must import it under a different mechanism — do not rely on both in one
process.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys
import types

# vendor/ lives next to the coffer/ package at the repository root.
_VENDOR_X402_DIR = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "x402"

#: How the spec module was resolved (set by :func:`resolve_spec_module`):
#: ``"top-level"`` — ``import x402`` already binds the vendored spec
#: (venv ``.pth`` active); ``"facade-loaded"`` — the facade registered the
#: vendored copy as ``sys.modules["x402"]`` itself.
SPEC_SOURCE: str = ""


def _is_vendored_spec(mod: types.ModuleType | None) -> bool:
    """True when ``mod`` exposes the vendored spec surface (not the Coinbase SDK).

    The official x402 SDK (v2.x) does not provide ``build_payment_required``
    / ``X402Handler`` / ``FacilitatorRouter`` — those are the vendored
    x402-py surface this repository asserts as the source of truth
    (``docs/X402-SPEC-CONTRACT.md`` §G10).
    """
    return bool(
        mod is not None
        and hasattr(mod, "build_payment_required")
        and hasattr(mod, "X402Handler")
        and hasattr(mod, "FacilitatorRouter")
    )


def resolve_spec_module() -> types.ModuleType:
    """Return the vendored x402 spec implementation as a module object.

    Resolution order:

    1. ``sys.modules["x402"]`` already holds the vendored spec → reuse it.
    2. ``import x402`` resolves to the vendored spec (venv ``.pth``) → use it.
    3. Otherwise load ``vendor/x402`` from the repository checkout and
       register it as top-level ``x402`` ([REVIEW] shadows any installed
       official SDK for the rest of the process).
    """
    global SPEC_SOURCE

    current = sys.modules.get("x402")
    if _is_vendored_spec(current):
        SPEC_SOURCE = "top-level"
        return current  # type: ignore[return-value]

    if current is None:
        try:
            imported = importlib.import_module("x402")
        except ImportError:
            imported = None
        if _is_vendored_spec(imported):
            SPEC_SOURCE = "top-level"
            return imported  # type: ignore[return-value]

    init_py = _VENDOR_X402_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "x402",
        init_py,
        submodule_search_locations=[str(_VENDOR_X402_DIR)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
        raise ImportError(f"Coffer: vendored x402 spec not found at {init_py}")
    loaded = importlib.util.module_from_spec(spec)
    # Drop any stale submodules of a previously-active `x402` provider (e.g.
    # the official Coinbase SDK imported earlier in this process): otherwise
    # `from x402.facilitator import ...` inside the vendored package would
    # bind to the cached SDK submodules. Part of the documented shadowing
    # contract (see module docstring).
    for _name in [n for n in sys.modules if n == "x402" or n.startswith("x402.")]:
        del sys.modules[_name]
    # Register BEFORE exec so the vendored package's own `from x402 import ...`
    # lines (executed during __init__ submodule imports) resolve to it.
    sys.modules["x402"] = loaded
    try:
        spec.loader.exec_module(loaded)
    except BaseException:
        # Never leave a half-initialized module registered.
        if sys.modules.get("x402") is loaded:
            del sys.modules["x402"]
        raise
    SPEC_SOURCE = "facade-loaded"
    return loaded
