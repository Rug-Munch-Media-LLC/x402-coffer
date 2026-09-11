"""Coffer runtime configuration — centralized, fail-closed Settings.

Single source of truth for every env var the Coffer codebase reads
(``X402_*``, ``REDIS_URL``, ``WALLET_VAULT_PASSWORD``, …). Modules that today
read ``os.getenv`` directly (e.g. :mod:`app.domain.x402.service`,
``app/domain/x402/store.py``) may migrate onto :class:`X402Settings` over time;
this module is the canonical declaration *and* the boot-time validation gate,
so a misconfigured merchant fails fast with a clear message at startup instead
of silently serving an unconfigured wallet.

Fails closed: if a REQUIRED wallet is missing/unset, :func:`load_settings`
raises with a human-readable list of what to set — it never returns a
half-configured object.

Deliberately does **not** depend on ``pydantic-settings`` (not a package dep,
and not in the shipped subset); it uses plain ``pydantic.BaseModel`` + explicit
env parsing so the container cannot fail on an undeclared optional package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Brand / version mirror (kept local to avoid a circular import into the facade).
__version__ = "5.0.0"

#: Merchant must set at least one receiving wallet. Everything else can fall
#: back to conservative defaults — an unset wallet is the one fatal misconfig.
REQUIRED: tuple[str, ...] = ("X402_WALLET",)

#: Unconfigured-wallet sentinels. NOTE: the operator EVM treasury is a REAL
#: payee address and must NEVER be listed here — doing so made
#: ``wallet_configured()`` return False when X402_WALLET was set to the
#: treasury, refusing to boot a correctly configured deployment.
_UNCONFIGURED_MARKERS = {"", "coffer-unconfigured-wallet"}

#: Facilitator signing key env vars. The key NEVER lives in code: it is read
#: from ``X402_SELF_FACILITATOR_KEY`` (env) or, when unset, from the secret
#: FILE named by ``X402_SELF_FACILITATOR_KEY_FILE``. A deployment that needs
#: to sign settlements (self-hosted + RPC configured) but has neither fails
#: fast at boot instead of silently returning ``None`` from every settle.
SIGNING_KEY_ENV = "X402_SELF_FACILITATOR_KEY"
SIGNING_KEY_FILE_ENV = "X402_SELF_FACILITATOR_KEY_FILE"

# ── TTL defaults (audit #54) ─────────────────────────────────────────────────
# Payment/nonce TTLs were previously hardcoded in several modules with
# DRIFTING defaults (300s in facilitator_config, 3600s in store/config).
# They now live here as the single source of truth; every consumer reads
# these constants as its fallback default and X402_* env vars override.
#: Verified-payment validity window (seconds). Env: X402_PAYMENT_TTL.
DEFAULT_PAYMENT_TTL: int = 3600
#: Replay-nonce retention window (seconds). Env: X402_NONCE_TTL.
DEFAULT_NONCE_TTL: int = 86400

_deprecated_env_warned: set[str] = set()


def _warn_deprecated_env_once(alias: str, canonical: str) -> None:
    """Log a one-time deprecation warning for a legacy env alias (audit #57)."""
    if alias in _deprecated_env_warned:
        return
    _deprecated_env_warned.add(alias)
    import logging

    logging.getLogger(__name__).warning(
        "x402_env_deprecated: %s is deprecated; use %s (alias accepted, will be removed)",
        alias,
        canonical,
    )


def _canonical_env(e: Mapping[str, str], canonical: str, *aliases: str, default: str = "") -> str:
    """Resolve an env var canonical-first; deprecated aliases warn once.

    Audit #57: env naming was mixed-case / mixed-prefix (``X402_burst_limit``,
    legacy ``COFFER_*`` names). Canonical names are ``X402_*`` UPPER_SNAKE;
    aliases are still accepted read-time but emit a single deprecation warning.
    """
    val = e.get(canonical, "")
    if val:
        return val
    for alias in aliases:
        val = e.get(alias, "")
        if val:
            _warn_deprecated_env_once(alias, canonical)
            return val
    return default


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on", "t", "y")


class X402Settings(BaseModel):
    """Canonical Coffer runtime settings (a single validated snapshot)."""

    # ── Receiving wallets / networks ─────────────────────────────────────
    pay_to: str = ""
    wallet: str = ""
    wallet_base: str = ""
    wallet_eth: str = ""
    wallet_arbitrum: str = ""
    wallet_optimism: str = ""
    wallet_polygon: str = ""
    wallet_tron: str = ""
    wallet_solana: str = ""
    evm_pay_to: str = ""
    sol_pay_to: str = ""
    btc_pay_to: str = ""
    tron_pay_to: str = ""
    asterpay_sepa_iban: str = ""

    # ── Self-hosted facilitation / settlement ─────────────────────────────
    self_facilitate: bool = True
    self_facilitator_key: str = ""
    self_facilitator_key_file: str = ""
    self_facilitator_rpc: str = ""
    offline_mode: bool = False
    redis_url: str = ""
    nonce_ttl: int = 86400

    # ── Facilitator routing (third-party / external) ───────────────────────
    coinbase_enabled: bool = True
    coinbase_verify: str = "https://x402.org/facilitator/verify"
    coinbase_settle: str = "https://x402.org/facilitator/settle"
    coinbase_priority: int = 1
    coinbase_api_key: str = ""
    payai_enabled: bool = True
    payai_verify: str = "https://facilitator.payai.network/verify"
    payai_settle: str = "https://facilitator.payai.network/settle"
    payai_priority: int = 2
    cf_enabled: bool = False
    cf_verify: str = ""
    cf_settle: str = ""
    cf_priority: int = 3
    facilitator_url: str = "https://facilitator.payai.network"
    default_network: str = "eip155:8453"

    # ── Protocol / expiry / validation tuning ─────────────────────────────
    timeout_seconds: float = 15.0
    payment_ttl: int = 3600
    solana_rpc: str = ""
    eip712_name: str = "USD Coin"
    eip712_version: str = "2"
    pricing_json: str = ""

    # ── Enforcement / burst (legacy routers) ──────────────────────────────
    burst_limit: int = 5
    burst_window: int = 60
    wallet_vault_password: str = ""
    rmi_mcp_api_key: str = ""

    # ── Operator UX for the deployable FastAPI app ────────────────────────
    # Binds behind nginx/cloudflared; operator-overridable via X402_HOST.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8001
    debug: bool = False

    #: Path prefixes exempt from the 402 gate. Narrowed (audit H3): ONLY the
    #: x402 protocol endpoints a payer must reach BEFORE paying, plus liveness/
    #: readiness + self-description routes. The broad ``/v1/x402`` prefix is
    #: gone (it freed ``/v1/x402/agents``, ``/v1/x402/metrics``, ``/v1/x402/
    #: bazaar``), and the revenue/ops surfaces (``/dashboard``, ``/metrics``,
    #: agent registry, observability metrics) are now payment/auth-gated.
    free_paths: frozenset[str] = frozenset(
        {
            # x402 protocol endpoints (pre-payment by design).
            "/v1/x402/payment",
            "/v1/x402/verify",
            "/v1/x402/settle",
            "/v1/x402/health",
            # Liveness / readiness / self-description — no revenue data.
            "/health",
            "/healthz",
            "/readyz",
            "/docs",
            "/openapi",
            "/version",
            "/supported",
        }
    )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> X402Settings:
        """Build settings from the process (or an explicit mapping) environ."""
        e = os.environ if env is None else env

        def g(name: str, default: str = "") -> str:
            return e.get(name, default)

        def first(*names: str) -> str:
            """Canonical-first env alias resolution (first non-empty wins)."""
            for name in names:
                val = e.get(name, "")
                if val:
                    return val
            return ""

        def _secret(env_names: tuple[str, ...], file_env: str) -> tuple[str, str]:
            """Resolve a secret: env var first, then a secret FILE.

            Returns ``(secret_value, file_path)``. Reading a secret from a
            mounted file (Docker/K8s secret, gopass export) keeps the material
            out of the process environment and out of git; ``""`` means the
            secret is unset.
            """
            for name in env_names:
                val = e.get(name, "")
                if val:
                    return val, ""
            file_path = e.get(file_env, "").strip()
            if not file_path:
                return "", ""
            try:
                return Path(file_path).read_text(encoding="utf-8").strip(), file_path
            except OSError:
                return "", file_path

        signing_key, signing_key_file = _secret((SIGNING_KEY_ENV,), SIGNING_KEY_FILE_ENV)

        return cls(
            pay_to=g("X402_PAY_TO", "").lower(),
            # Canonical env schema: X402_WALLET (EVM), X402_WALLET_SOLANA,
            # X402_WALLET_TRON. Old names are read-time aliases only.
            wallet=first("X402_WALLET", "X402_PAY_TO", "X402_EVM_PAY_TO", "X402_BASE_WALLET").lower(),
            wallet_base=g("X402_WALLET_BASE", "").lower(),
            wallet_eth=g("X402_WALLET_ETH", "").lower(),
            wallet_arbitrum=g("X402_WALLET_ARBITRUM", "").lower(),
            wallet_optimism=g("X402_WALLET_OPTIMISM", "").lower(),
            wallet_polygon=g("X402_WALLET_POLYGON", "").lower(),
            wallet_tron=first("X402_WALLET_TRON", "X402_TRON_PAY_TO").lower(),
            wallet_solana=first("X402_WALLET_SOLANA", "X402_SOL_PAY_TO", "X402_SOLANA_WALLET").strip(),
            evm_pay_to=first("X402_EVM_PAY_TO", "X402_WALLET").lower(),
            sol_pay_to=first("X402_SOL_PAY_TO", "X402_WALLET_SOLANA").strip(),
            btc_pay_to=g("X402_BTC_PAY_TO", "").strip(),
            tron_pay_to=first("X402_TRON_PAY_TO", "X402_WALLET_TRON").strip(),
            asterpay_sepa_iban=g("ASTERPAY_SEPA_IBAN", "").strip(),
            self_facilitate=_env_bool("X402_SELF_FACILITATE", True),
            self_facilitator_key=signing_key,
            self_facilitator_key_file=signing_key_file,
            self_facilitator_rpc=g("X402_SELF_FACILITATOR_RPC", ""),
            offline_mode=(
                g("X402_OFFLINE_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "t", "y")
                and g("X402_DEV_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "t", "y")
            ),
            redis_url=g("X402_REDIS_URL", g("REDIS_URL", "")),
            nonce_ttl=int(g("X402_NONCE_TTL", str(DEFAULT_NONCE_TTL))),
            coinbase_enabled=_env_bool("X402_COINBASE_ENABLED", True),
            coinbase_verify=g("X402_COINBASE_VERIFY", "https://x402.org/facilitator/verify"),
            coinbase_settle=g("X402_COINBASE_SETTLE", "https://x402.org/facilitator/settle"),
            coinbase_priority=int(g("X402_COINBASE_PRIORITY", "1")),
            coinbase_api_key=g("X402_COINBASE_API_KEY", ""),
            payai_enabled=_env_bool("X402_PAYAI_ENABLED", True),
            payai_verify=g("X402_PAYAI_VERIFY", "https://facilitator.payai.network/verify"),
            payai_settle=g("X402_PAYAI_SETTLE", "https://facilitator.payai.network/settle"),
            payai_priority=int(g("X402_PAYAI_PRIORITY", "2")),
            cf_enabled=_env_bool("X402_CF_ENABLED", False),
            cf_verify=g("X402_CF_VERIFY", ""),
            cf_settle=g("X402_CF_SETTLE", ""),
            cf_priority=int(g("X402_CF_PRIORITY", "3")),
            facilitator_url=g("X402_FACILITATOR_URL", "https://facilitator.payai.network"),
            default_network=g("X402_DEFAULT_NETWORK", "eip155:8453"),
            timeout_seconds=float(g("X402_TIMEOUT_SECONDS", "15")),
            payment_ttl=int(g("X402_PAYMENT_TTL", str(DEFAULT_PAYMENT_TTL))),
            solana_rpc=g("X402_SOLANA_RPC", ""),
            eip712_name=g("X402_EIP712_NAME", "USD Coin"),
            eip712_version=g("X402_EIP712_VERSION", "2"),
            pricing_json=g("X402_PRICING_JSON", ""),
            burst_limit=int(
                _canonical_env(e, "X402_BURST_LIMIT", "X402_burst_limit", "COFFER_BURST_LIMIT", default="5")
            ),
            burst_window=int(
                _canonical_env(
                    e, "X402_BURST_WINDOW", "X402_burst_window", "COFFER_BURST_WINDOW", default="60"
                )
            ),
            wallet_vault_password=_canonical_env(
                e, "X402_WALLET_VAULT_PASSWORD", "WALLET_VAULT_PASSWORD", "COFFER_WALLET_VAULT_PASSWORD"
            ),
            rmi_mcp_api_key=_canonical_env(
                e, "X402_RMI_MCP_API_KEY", "RMI_MCP_API_KEY", "COFFER_RMI_MCP_API_KEY"
            ),
            # Default bind, fronted by nginx/cloudflared.
            host=g("X402_HOST", "0.0.0.0"),  # nosec B104
            port=int(g("X402_PORT", "8001")),
            debug=_env_bool("X402_DEBUG", False),
        )

    @property
    def network_wallets(self) -> dict[str, str]:
        """CAIP-2 id -> receiving address, mirroring service.X402_NETWORK_WALLETS."""
        base = self.wallet_base or self.wallet or self.evm_pay_to
        return {
            "eip155:8453": base,
            "eip155:1": self.wallet_eth or self.wallet,
            "eip155:42161": self.wallet_arbitrum or self.wallet,
            "eip155:10": self.wallet_optimism or self.wallet,
            "eip155:137": self.wallet_polygon or self.wallet,
            "solana:mainnet": self.wallet_solana,
            "tron:mainnet": self.wallet_tron,
        }

    @property
    def wallet_configured(self) -> bool:
        """Any real receiving wallet present (placeholder / empty disallowed)."""
        candidates = [self.wallet, self.pay_to] + list(self.network_wallets.values())
        return any(str(c).strip().lower() not in _UNCONFIGURED_MARKERS for c in candidates)

    def assert_configured(self) -> X402Settings:
        """Fail closed when the runtime cannot accept OR settle payments.

        1. A receiving wallet is required (unset wallet -> refuse to boot).
        2. In production (``debug`` off), a self-hosted deployment that has an
           RPC configured but NO facilitator signing key fails fast — settle
           would otherwise silently return ``None`` from every broadcast.
           The key must come from ``X402_SELF_FACILITATOR_KEY`` or the secret
           file ``X402_SELF_FACILITATOR_KEY_FILE`` — never from code.
        """
        missing = [name for name in REQUIRED if not self.wallet_configured]
        if missing:
            raise RuntimeError(
                "Coffer is NOT configured to accept payments. Set at least one "
                "receiving wallet, e.g. X402_WALLET=<evm address> (Base USDC) or "
                "X402_WALLET_SOLANA=<solana base58>. "
                "Full reference: see .env.example / docs/COFFER-DEPLOY.md "
                f"[missing/invalid: {', '.join(REQUIRED)}]"
            )
        if (
            not self.debug
            and self.self_facilitate
            and not self.offline_mode
            and bool(self.self_facilitator_rpc.strip())
            and not self.self_facilitator_key
        ):
            raise RuntimeError(
                "Coffer is configured for self-hosted settlement "
                f"(X402_SELF_FACILITATOR_RPC={self.self_facilitator_rpc!r}) but the "
                "facilitator signing key is unset. Set X402_SELF_FACILITATOR_KEY "
                "or X402_SELF_FACILITATOR_KEY_FILE (secret file) — the key is "
                "never read from code."
            )
        return self


#: Process-wide settings (built + validated once). Modules/tests may supply an
#: explicit mapping instead of the process environ via :func:`load_settings`.
settings: X402Settings | None = None


def load_settings(env: dict[str, str] | None = None) -> X402Settings:
    """Build (and cache) the canonical, validated Coffer settings snapshot."""
    global settings
    if settings is None or env is not None:
        built = X402Settings.from_env(env)
        if env is not None:
            return built
        built.assert_configured()
        settings = built
    assert settings is not None
    return settings


def get_settings(req: Any = None) -> X402Settings:
    """FastAPI dependency: cached validated settings (req ignored)."""
    return load_settings()


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Return a deployable FastAPI app exposing the /v1/x402 HTTP surface.

    Thin factory assembled from existing, unmodified domain modules — no new
    route logic lives here. It validates settings (fail-closed on a missing
    wallet) and mounts the spec-canonical 402/verify/settle/health router
    behind :class:`X402Middleware`, freeing only the protocol + meta paths.
    Imported lazily so this module stays importable without FastAPI loaded.
    """
    # Fail fast at boot on a missing wallet — never serve an unconfigured 402.
    cfg = load_settings()

    from fastapi import FastAPI

    from coffer._engine.extensions import register_all_extensions
    from coffer._engine.middleware import X402Middleware
    from coffer._engine.router import health_router
    from coffer._engine.router import router as x402_router

    # Wire every extension entry point (payment-identifier idempotency,
    # gas-sponsoring, scheme stubs) onto the global facilitator router.
    register_all_extensions()

    app = FastAPI(
        title="Coffer — self-hosted x402 payment facilitator",
        description="The x402 HTTP treasury. Request a 402, pay USDC, replay with X-Payment-ID.",
        version=__version__,
    )
    app.include_router(x402_router)
    # Liveness probe (store + redis) mounted at the top level /health.
    app.include_router(health_router)

    # Bazaar discovery: browse catalog of declared paid resources.
    from coffer._engine.extensions.bazaar import register_bazaar_router

    app.include_router(register_bazaar_router())

    # Settlement observability: metrics + ledger snapshot.
    from coffer._engine.observability import register_metrics_router

    app.include_router(register_metrics_router())

    # Read-only ops dashboard.
    from coffer._engine.dashboard import register_dashboard_router

    app.include_router(register_dashboard_router())

    # Agent identity browse surface (ERC-8004 / a2a-x402 shaped).
    from coffer._engine.extensions.agent_identity import register_agents_router

    app.include_router(register_agents_router())

    # Customer billing surface: settlement history + prepaid balance.
    from coffer._engine.billing import router as billing_router

    app.include_router(billing_router)

    # x402 V2 spec ops surface: /supported, /healthz, /readyz.
    from coffer.mcp import ops_router

    app.include_router(ops_router)

    app.add_middleware(
        X402Middleware,
        free_paths=set(cfg.free_paths),
    )

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "product": "Coffer",
            "version": __version__,
            "networks": sorted(cfg.network_wallets.keys()),
            "wallet_configured": cfg.wallet_configured,
            "x402": "/v1/x402/payment",
            "docs": "/docs",
        }

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return {"name": "Coffer", "version": __version__}

    return app


__all__ = [
    "REQUIRED",
    "X402Settings",
    "create_app",
    "get_settings",
    "load_settings",
    "settings",
]
