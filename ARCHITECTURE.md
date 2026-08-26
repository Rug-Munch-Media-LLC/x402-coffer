# Coffer Architecture

> **Coffer** is the self-hosted x402 payment facilitator. It accepts `402 Payment Required` micropayments in one line of Python, on your own infrastructure — your HTTP treasury, sovereign and open.

---

## Table of Contents

1. [Design Principles](#design-principles)
2. [Module Layering](#module-layering)
3. [Payment Flow](#payment-flow)
4. [Verification Pipeline](#verification-pipeline)
5. [Settlement Pipeline](#settlement-pipeline)
6. [Multi-Chain Architecture](#multi-chain-architecture)
7. [Extension System](#extension-system)
8. [Observability](#observability)
9. [Deployment Topology](#deployment-topology)
10. [Security Architecture](#security-architecture)

---

## Design Principles

1. **Sovereign payments.** No external facilitator is required. Every settlement path defaults to **your own signature** (EIP-3009 on EVM, SPL `transferChecked` on Solana, TRC-20 Transfer on TRON).

2. **Fail-closed.** Every verification path defaults to **unpaid** on any ambiguity. Missing payment → 402. Expired → 402. RPC timeout → 402. Malformed response → 402.

3. **Server-side pricing is authoritative.** Client-supplied amounts are validated against the server-side price table. Underpayment is rejected before any on-chain call.

4. **Honest security posture.** Chains without real settlement verification are explicitly reported as scaffolded. Never fake security.

5. **Zero vendor lock-in.** Wire format is the open x402 standard; the facilitator is swappable behind `FacilitatorRouter`.

---

## Module Layering

```
coffer/                          # Public facade — one import surface
├── __init__.py                  # Re-exports everything (flat public API)
├── mcp.py                       # MCP tool manifests + paid server
├── spec.py                      # Vendored x402-py reference implementation
├── extensions/                  # Optional spec extensions
│   ├── __init__.py
│   ├── payment_identifier.py    # Idempotent, caller-bound, TTL'd payment ids
│   ├── eip2612_gas_sponsoring.py # Gasless permit-style payments
│   ├── erc20_approval_gas_sponsoring.py # Gasless ERC-20 approve
│   └── upto.py                  # Authorize up to a maximum (scaffolded)
├── _engine/                     # Internal implementation (private)
│   ├── __init__.py
│   ├── facilitator_router.py    # Health-aware facilitator with auto-fallback
│   ├── facilitator_config.py    # X402_NETWORK_WALLETS, pricing, etc.
│   ├── self_facilitator.py      # EIP-3009 verify + settlement broadcast
│   ├── evm_verify.py            # EIP-7702 self-verify fallback
│   ├── solana_verifier.py       # SPL RPC verifier (getTransaction jsonParsed)
│   ├── tron_verifier.py         # TRC-20 Grid verifier (gettransactioninfobyid)
│   ├── middleware.py             # X402Middleware, require_payment decorator
│   ├── replay.py                # Nonce store + replay protection
│   ├── pricing.py               # Server-side pricing oracle
│   ├── assets.py                # SUPPORTED_ASSETS table (CAIP-2 keyed)
│   ├── models.py                # Pydantic models for wire format
│   ├── body.py                  # PaymentRequired / PaymentPayload / SettlementResponse
│   ├── router.py                # /v1/x402 REST endpoints
│   ├── dashboard.py             # Analytics + observability endpoints
│   ├── store.py                 # Payment record persistence (JSONL)
│   ├── service.py               # High-level service facade
│   ├── webhooks.py              # Payment event webhooks
│   ├── ratelimit.py             # Rate limiting middleware
│   ├── observability.py         # OpenTelemetry tracing + Prometheus metrics
│   ├── adapters.py              # FastAPI / Starlette / Flask adapters
│   ├── adapters_wsgi.py         # WSGI (Flask) adapter
│   ├── config.py                # Configuration loading
│   ├── manifest.py              # MCP tool manifest generation
│   ├── tools/                   # MCP tool implementations
│   └── extensions/              # Extension implementations
├── _spec.py                     # Spec import resolver (vendored x402-py)
├── docs/                        # Documentation
│   ├── x402-official-spec/      # Vendored official spec
│   └── COFFER-CHANGELOG.md      # Build record
├── tests/                       # Test suite
├── pyproject.toml               # Package metadata + dependencies
├── README.md                    # Product overview
├── SECURITY.md                  # Security policy
├── ARCHITECTURE.md              # This file
├── LICENSE                      # MIT License
└── CHANGELOG.md                 # Release notes
```

---

## Payment Flow

```
Client (Buyer)                     Server (Seller)
  │                                    │
  │  GET /premium                     │
  ├───────────────────────────────────►│
  │                                    │
  │  HTTP 402 Payment Required        │
  │  PAYMENT-REQUIRED: <base64 JSON>  │   ← price, network, asset, payTo
  │◄───────────────────────────────────┤
  │                                    │
  │  (client signs / sends payment)   │
  │                                    │
  │  GET /premium                     │
  │  PAYMENT-SIGNATURE: <base64 JSON> │   ← paymentId + tx hash / authorization
  ├───────────────────────────────────►│
  │                                    │
  │                              ┌─────┴──────┐
  │                              │  Facilitator │
  │                              │   Router    │
  │                              └─────┬──────┘
  │                                    │
  │                          ┌─────────┴─────────┐
  │                          │                   │
  │                    ┌─────┴─────┐      ┌──────┴──────┐
  │                    │  External  │      │  Self-Hosted │
  │                    │  Facilitator│      │  Facilitator │
  │                    │  (Coinbase, │      │  (EIP-3009,  │
  │                    │   PayAI,    │      │   SPL, TRC-20│
  │                    │   Cloudflare│      │   verify)    │
  │                    └─────┬──────┘      └──────┬───────┘
  │                          │                   │
  │                          └─────────┬─────────┘
  │                                    │
  │                          ┌─────────┴─────────┐
  │                          │   Verification    │
  │                          │   - EIP-7702 (EVM)│
  │                          │   - SPL RPC (Sol) │
  │                          │   - TRC-20 (TRON) │
  │                          └─────────┬─────────┘
  │                                    │
  │                          ┌─────────┴─────────┐
  │                          │    Settlement     │
  │                          │   - EIP-3009 sig  │
  │                          │   - Broadcast tx  │
  │                          │   - Return hash   │
  │                          └─────────┬─────────┘
  │                                    │
  │  HTTP 200 OK                      │
  │  PAYMENT-RESPONSE: <base64 JSON>  │   ← settlement receipt
  │◄───────────────────────────────────┤
```

---

## Verification Pipeline

The facilitator router selects the best available verification path:

```
verify(payment_id, tx_hash, network, asset, amount_usd)
  │
  ├─ X402_OFFLINE_MODE? → return verified=True (dev-only)
  │
  ├─ self_hosted or impl_for(network)?
  │   │
  │   ├─ _verify_self_hosted()
  │   │   │
  │   │   ├─ EVM + EIP7702_RPC? → _eip7702_verify()
  │   │   │   └─ eth_getTransactionReceipt → check logs for Transfer to payTo
  │   │   │
  │   │   ├─ Solana? → _verify_solana()
  │   │   │   └─ getTransaction jsonParsed → find USDC transferChecked to wallet
  │   │   │
  │   │   └─ TRON? → _verify_tron()
  │   │       └─ gettransactioninfobyid → decode TRC-20 Transfer event
  │   │
  │   └─ Register custom impl for network
  │
  ├─ External facilitator (coinbase, payai, cloudflare)
  │   └─ POST /verify → health-aware auto-fallback
  │
  └─ EIP-7702 self-verify fallback (EVM only)
      └─ Direct RPC: eth_getTransactionReceipt + log decode
```

### Verification Result Shape

```python
{
    "verified": bool,
    "reason": str,              # "" on success, error description on failure
    "settlement": dict | None,  # Settlement receipt (verify-only paths return None)
    "network": str,             # Normalized CAIP-2 network id
    "facilitator": str,         # "self-hosted", "solana-rpc", "tron-grid", "coinbase", etc.
    "signer": str,              # Payer address (when available)
    "amount_atomic": int,       # Amount in atomic units
    "asset": str,               # Asset symbol (USDC, USDT, SOL, TRX, etc.)
    "mint": str,                # Token mint/contract address
    "contract": str,            # Token contract address (Solana/TRON)
    "confirmed_at": str,        # ISO-8601 confirmation timestamp
    "amount_usd": float,        # USD value of payment
    "block": int | None,        # Block number (when available)
}
```

---

## Settlement Pipeline

### EVM Settlement (self_facilitator.py)

```
settle(payment_id, tx_hash, network, asset, amount_usd)
  │
  ├─ Idempotency check
  │   └─ Already settled? → return existing tx hash (never double-submit)
  │
  ├─ Build calldata
  │   ├─ With authorization → transferWithAuthorization(...)
  │   └─ Pre-approved → transferFrom(payer, payTo, value)
  │
  ├─ RPC calls (eth_*)
  │   ├─ eth_chainId → verify correct chain
  │   ├─ eth_getTransactionCount → get nonce
  │   ├─ eth_estimateGas → get gas limit
  │   ├─ eth_gasPrice → get gas price
  │   └─ eth_sendRawTransaction → broadcast
  │
  ├─ Sign + broadcast
  │   └─ EIP-155 signed legacy transaction
  │
  └─ Return tx hash
```

### Error Taxonomy

| Error Code | Meaning | Action |
| ---------- | ------- | ------ |
| `NOT_SETTLABLE` | No authorization, no pre-approval, wrong chain | Fail closed |
| `WRONG_CHAIN` | RPC chain ID ≠ expected | Fail closed |
| `RPC_ERROR` | RPC timeout, network error | Retry / fail closed |
| `NONCE_CONFLICT` | Nonce already used | Retry with new nonce |
| `INSUFFICIENT_GAS` | Settlement key has no gas tokens | Alert operator |
| `BROADCAST_FAILED` | tx rejected by network | Retry / fail closed |

---

## Multi-Chain Architecture

### Chain Family Abstraction

```python
def network_family(network: str) -> str:
    """Return chain family: 'evm' | 'solana' | 'tron' | 'unknown'"""
```

### Per-Chain Verification

| Chain Family | Verifier | Method | SDK Required |
| ------------ | -------- | ------ | ------------ |
| **EVM** | EIP-7702 self-verify | `eth_getTransactionReceipt` + log decode | ❌ (httpx only) |
| **Solana** | SPL RPC verifier | `getTransaction` jsonParsed + `transferChecked` walk | ❌ (httpx only) |
| **TRON** | TRC-20 Grid verifier | `gettransactioninfobyid` + Transfer event decode | ❌ (httpx only) |

### Per-Chain Settlement

| Chain Family | Settlement Method | Gas Payer |
| ------------ | ----------------- | --------- |
| **EVM** | `transferWithAuthorization` broadcast | Server (hot key) |
| **Solana** | SPL transfer (buyer-side, verified only) | Buyer |
| **TRON** | TRC-20 transfer (buyer-side, verified only) | Buyer |

---

## Extension System

Coffer implements the 2026 x402 extensions:

| Extension | Status | Module |
| --------- | ------ | ------ |
| **payment-identifier** | ✅ implemented | `extensions/payment_identifier.py` |
| **eip2612-gas-sponsoring** | ✅ implemented | `extensions/eip2612_gas_sponsoring.py` |
| **erc20-approval-gas-sponsoring** | ✅ implemented | `extensions/erc20_approval_gas_sponsoring.py` |
| **upto** scheme | 🔜 scaffolded | `extensions/upto.py` |
| **batch-settlement** | 🔜 scaffolded | `extensions/batch_settlement.py` |

### Payment-Identifier Extension

Provides idempotent, caller-bound, TTL'd payment IDs:

1. Server advertises extension in `PaymentRequired`
2. Client generates unique payment ID
3. Server caches response keyed by payment ID
4. Retries with same ID return cached response (no double-payment)

---

## Observability

### Logging

All verification attempts (success and failure) are structured-logged:

```json
{
  "event": "x402_verify",
  "payment_id": "pay_abc123",
  "tx_hash": "0x...",
  "network": "eip155:8453",
  "asset": "USDC",
  "verified": true,
  "facilitator": "self-hosted",
  "amount_usd": 0.001,
  "caller_key": "10.0.0.1",
  "elapsed_ms": 45.2
}
```

### Metrics (Prometheus)

| Metric | Type | Labels | Description |
| ------ | ---- | ------ |-------------|
| `x402_verify_total` | counter | `network`, `asset`, `result` | Total verification attempts |
| `x402_verify_duration` | histogram | `network`, `asset` | Verification latency |
| `x402_settle_total` | counter | `network`, `asset`, `result` | Total settlement attempts |
| `x402_settle_duration` | histogram | `network`, `asset` | Settlement latency |
| `x402_facilitator_health` | gauge | `facilitator` | 1 if healthy, 0 if failing |
| `x402_replay_rejected_total` | counter | `network` | Replay attacks rejected |

### Health Endpoint

```
GET /health
```

```json
{
  "status": "ok",
  "version": "5.0.0",
  "facilitator": "self-hosted",
  "oracle": {
    "reachable": true,
    "source": "coingecko"
  },
  "supported_networks": ["eip155:8453", "eip155:1", "..."],
  "supported_assets_count": 25,
  "verification_status": {
    "evm": "live",
    "solana": "live",
    "tron": "live"
  },
  "settlement": {
    "total_payments": 1234,
    "pending": 2,
    "settled": 1230,
    "failed": 2
  }
}
```

---

## Deployment Topology

### Single-Process (Default)

```
┌─────────────────────────────────────────┐
│           FastAPI / Starlette           │
│  ┌───────────────────────────────────┐  │
│  │        X402Middleware             │  │
│  │  ┌─────────────────────────────┐  │  │
│  │  │    FacilitatorRouter        │  │  │
│  │  │  ┌───────────────────────┐  │  │  │
│  │  │  │   SelfFacilitator     │  │  │  │
│  │  │  │  - EIP-3009 verify    │  │  │  │
│  │  │  │  - SPL RPC verify     │  │  │  │
│  │  │  │  - TRC-20 Grid verify │  │  │  │
│  │  │  └───────────────────────┘  │  │  │
│  │  └─────────────────────────────┘  │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### Standalone Facilitator (Production)

```
┌──────────────┐         ┌──────────────────────┐
│  App Server  │ ──────► │  Coffer Facilitator  │
│  (your API)  │  HTTP   │  - /supported        │
└──────────────┘         │  - /verify           │
                         │  - /settle           │
                         │  - /health           │
                         └──────────────────────┘
```

### Multi-Worker (Redis-backed)

```
┌──────────────┐         ┌──────────────────────┐         ┌──────────┐
│  App Server  │ ──────► │  Coffer Facilitator  │ ◄─────► │  Redis   │
│  (your API)  │  HTTP   │  (multiple workers)  │  SETNX  │  Nonces  │
└──────────────┘         └──────────────────────┘         └──────────────┘
```

---

## Security Architecture

### Threat Model

See [SECURITY.md](SECURITY.md) for full threat model and mitigation strategies.

### Key Security Properties

1. **Replay hardening** (three layers):
   - Payment TTL (default 1h, configurable)
   - Caller IP binding
   - EIP-3009 nonce store (in-memory or Redis SETNX)

2. **Server-side pricing is authoritative.** Client-supplied `amount_usd` is validated against the server-side price table.

3. **No silent failures.** Every error path returns `verified: false` with a reason.

4. **Scaffolded chains never report verified.** VERIFICATION_STATUS is explicit.

5. **Fail-closed on misconfigured routes.** Every `/v1/*` endpoint is classified as free or paid; misclassifications default to paid.

### Input Validation

All inputs are validated with Pydantic v2:

- `payment_id`: non-empty string, max 64 chars
- `tx_hash`: non-empty string, hex or base58
- `network`: must be in SUPPORTED_NETWORKS (CAIP-2)
- `asset`: must be in SUPPORTED_ASSETS for the network
- `amount_usd`: non-negative finite float

### Secrets Management

Secrets are resolved in priority order:

1. Environment variable (`X402_WALLET`, `X402_FACILITATOR_KEY`, etc.)
2. Legacy env var (`PRY_X402_*`)
3. Pluggable secret resolver (`X402_SECRET_RESOLVER`)
4. Default (empty string → fail closed)

---

## See Also

- [README.md](README.md) — Product overview + quickstart
- [SECURITY.md](SECURITY.md) — Security policy + threat model
- [CHANGELOG.md](CHANGELOG.md) — Release notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — Development guide
- [docs/x402-official-spec/](docs/x402-official-spec/) — Vendored official spec
