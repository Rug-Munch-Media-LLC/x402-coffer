<p align="center">
  <img src="https://img.shields.io/badge/coffer-x402%20facilitator-2ea44f?style=for-the-badge" alt="Coffer" />
</p>

<h1 align="center">Coffer — The Sovereign x402 Payment Facilitator</h1>

<p align="center">
  <strong>Pay-per-request HTTP for the multi-chain economy.<br/>
  Self-host your own payment gateway. Accept USDC, USDT, SOL, TRX — on EVM, Solana, TRON.<br/>
  No Coinbase. No accounts. No subscriptions. No API keys.</strong>
</p>

<p align="center">
  <a href="https://git.rugmunch.io/RugMunchMedia/x402-coffer/actions"><img src="https://img.shields.io/badge/build-passing-brightgreen" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-000000" /></a>
  <a href="https://www.x402.org/"><img src="https://img.shields.io/badge/x402-v2%20spec-2ea44f" /></a>
</p>

---

## Why Coffer?

Every AI agent, API, and microservice needs to accept payments. Today you have two choices:

1. **Coinbase's hosted facilitator** — free but not sovereign. They see every transaction. They can censor, rate-limit, or shut you down.
2. **Build your own** — months of work, EIP-3009 signing, multi-chain verification, replay hardening.

**Coffer is the third option**: a complete, open-source, self-hosted x402 facilitator that you control end-to-end. One line to add a paywall. One config file to go sovereign.

| Feature | Coffer | Coinbase CDP | PayAI | Fireblocks | Mogami |
| ------- | :----: | :----------: | :---: | :--------: | :----: |
| **Open source (MIT)** | ✅ | ❌ | ❌ | ✅ | ✅ |
| **Self-hosted** (no hosted dep) | ✅ | ❌ | ❌ | ⚠️ vault | ⚠️ Base-only |
| **Networks live** | **6** (EVM×5 + Solana + TRON) | 1 | multi | multi | 1 |
| **Live payment options** | **25+** | USDC-centric | all tokens | — | USDC |
| **Self-settle in-process** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **TRON TRC-20** | ✅ live | ❌ | — | ❌ | ❌ |
| **Solana SPL** | ✅ live | ❌ | — | ❌ | ❌ |
| **EIP-7702 self-verify** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Facilitator router + fallback** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **MCP server** | ✅ native | ❌ | ❌ | ❌ | ❌ |
| **REST + MCP dual interface** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Dashboard + webhooks** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **110+ data templates** | ✅ | ❌ | ❌ | ❌ | ❌ |

---

## Quick Start

### Install

```bash
pip install x402-coffer
```

### One-Line Paywall

```python
from coffer import require_payment

@app.get("/premium")
@require_payment(asset="USDC", network="base", amount=0.001, operation="scrape")
async def premium(request):
    return {"data": "you paid!"}
```

### Docker (Full Stack)

```bash
git clone https://git.rugmunch.io/RugMunchMedia/x402-coffer.git
cd x402-coffer
cp .env.example .env   # set your wallet + RPC
docker compose up
```

Your facilitator is live at `http://localhost:8000`. Dashboard at `/dashboard`.

---

## How It Works

```
Client (AI Agent / Browser)        Your Server (Coffer)
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
  │  PAYMENT-SIGNATURE: <base64 JSON> │
  ├───────────────────────────────────►│
  │                                    │
  │                              ┌─────┴──────┐
  │                              │ Facilitator │
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

## Multi-Chain Support

### Live Networks

| Network | CAIP-2 | Assets | Verification | Settlement |
| ------- | ------ | ------ | ------------ | ---------- |
| **Base** | `eip155:8453` | USDC, USDT, ETH, WETH | EIP-7702 self-verify + facilitators | EIP-3009 broadcast |
| **Ethereum** | `eip155:1` | USDC, USDT, ETH, WETH | EIP-7702 self-verify + facilitators | EIP-3009 broadcast |
| **Arbitrum** | `eip155:42161` | USDC, USDT, ARB, ETH, WETH | EIP-7702 self-verify + facilitators | EIP-3009 broadcast |
| **Optimism** | `eip155:10` | USDC, USDT, ETH, WETH | EIP-7702 self-verify + facilitators | EIP-3009 broadcast |
| **Polygon** | `eip155:137` | USDC, USDT, POL, WETH | EIP-7702 self-verify + facilitators | EIP-3009 broadcast |
| **Solana** | `solana:mainnet` | USDC, USDT, SOL | SPL RPC verifier (getTransaction jsonParsed) | Buyer-side (verified) |
| **TRON** | `tron:mainnet` | USDT, TRX | TRC-20 Grid verifier (gettransactioninfobyid) | Buyer-side (verified) |

### Honest Security Posture

Every chain reports its verification status programmatically:

```python
from coffer import VERIFICATION_STATUS
# {"evm": "live", "solana": "live", "tron": "live"}
```

Scaffolded chains (if any) are **never** reported as verified. No fake security.

---

## Architecture

```
coffer/
├── __init__.py              # Public API — one import surface
├── mcp.py                   # MCP server with paid tool surface
├── _engine/                 # Internal implementation
│   ├── facilitator_router.py    # Health-aware router with auto-fallback
│   ├── self_facilitator.py      # EIP-3009 verify + settlement broadcast
│   ├── evm_verify.py            # EIP-7702 self-verify fallback
│   ├── solana_verifier.py       # SPL RPC verifier (no SDK)
│   ├── tron_verifier.py         # TRC-20 Grid verifier (no SDK)
│   ├── middleware.py             # X402Middleware, require_payment
│   ├── handler.py               # X402Handler (verify/settle orchestration)
│   ├── batch.py                 # Batch payment helpers
│   ├── pricing.py               # Server-side pricing oracle
│   ├── assets.py                # SUPPORTED_ASSETS table (CAIP-2 keyed)
│   ├── body.py                  # PaymentRequired / PaymentPayload / SettlementResponse
│   ├── scheme.py                # Backward-compat re-exports
│   ├── replay.py                # Nonce store + replay protection
│   ├── store.py                 # Payment record persistence
│   ├── dashboard.py             # Analytics + observability
│   ├── webhooks.py              # Payment event webhooks
│   ├── ratelimit.py             # Rate limiting middleware
│   ├── observability.py         # OpenTelemetry + Prometheus
│   ├── adapters.py              # FastAPI / Starlette adapters
│   ├── adapters_wsgi.py         # WSGI (Flask) adapter
│   └── ...
├── tests/                     # Test suite
├── docs/                      # Documentation
├── compose.yml                # Docker deployment
├── pyproject.toml             # Package metadata
├── README.md                  # This file
├── SECURITY.md                # Security policy
├── ARCHITECTURE.md            # Full architecture
└── CHANGELOG.md               # Release notes
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full module layering, payment flow, verification pipeline, settlement pipeline, and deployment topologies.

---

## Configuration

All configuration is env-var-first with `X402_*` prefixes:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `X402_WALLET` | — | Your receiving wallet (EVM address) |
| `X402_WALLET_SOLANA` | — | Your Solana receiving address |
| `X402_WALLET_TRON` | — | Your TRON receiving address (base58) |
| `X402_SELF_FACILITATE` | `1` | Enable self-hosted settlement (no Coinbase) |
| `X402_FACILITATOR_KEY` | — | EVM private key for settlement gas |
| `X402_FACILITATOR_RPC` | — | EVM RPC endpoint for settlement |
| `X402_NETWORK` | `base` | Default network |
| `X402_ASSET` | `USDC` | Default asset |
| `X402_PAYMENT_TTL` | `3600` | Payment validity in seconds |
| `X402_PRICING_JSON` | — | Override pricing table (JSON) |

---

## API Reference

### Middleware (One-Line Paywall)

```python
from coffer import X402Middleware

app.add_middleware(X402Middleware, free_paths={"/health", "/docs"})
```

### Per-Route Decorator

```python
from coffer import require_payment

@app.get("/premium")
@require_payment(asset="USDC", network="base", amount=0.001, operation="scrape")
async def premium(request): ...
```

### Full-Control Handler

```python
from coffer import X402Handler

handler = X402Handler()

@app.get("/premium")
async def premium(request):
    payment = handler.decode_payment_signature(request.headers.get("PAYMENT-SIGNATURE"))
    if not payment:
        return handler.require_payment("scrape", resource=str(request.url))
    result = await handler.verify_payment(payment["paymentId"], payment["txHash"], network="base")
    if not result["verified"]:
        return JSONResponse(status_code=402, content={"error": result["reason"]})
    settlement = await handler.settle_payment(payment["paymentId"], payment["txHash"], network="base")
    return JSONResponse(content={"data": "paid!"}, headers={"PAYMENT-RESPONSE": settlement})
```

### MCP Server

```python
from coffer.mcp import create_mcp_server

mcp = create_mcp_server(pricing={"/weather": 0.001, "/scrape": 0.01})
```

---

## Security

See [SECURITY.md](SECURITY.md) for the full security policy, threat model, and best practices.

Key principles:
- **Server-side pricing is authoritative** — client amounts are validated, never trusted
- **Fail-closed everywhere** — missing wallets, unknown assets, RPC timeouts all reject
- **Replay hardening** — TTL + caller binding + EIP-3009 nonce store
- **No silent failures** — every error returns `verified: false` with a reason
- **Honest status** — scaffolded chains never report as live

---

## Compliance

Coffer is a **non-custodial** facilitator — it never holds buyer funds. Settlement moves directly from buyer to seller via on-chain transfers.

- **OFAC/Sanctions**: Coffer does not perform KYT/OFAC screening. Operators in regulated jurisdictions must implement their own sanctions screening.
- **KYC/AML**: Non-custodial — operators should assess local requirements.
- **Audit trail**: All verification/settlement events are logged with payment_id, tx_hash, network, amount, caller, timestamp.

---

## FAQ

**Q: Do I need Coinbase?**
A: No. Coffer is fully self-hosted. You can optionally use Coinbase's facilitator as one of many backends, but it's never required.

**Q: What chains are supported?**
A: EVM (Base, Ethereum, Arbitrum, Optimism, Polygon), Solana, and TRON. 25+ live (network, asset) options.

**Q: Is there a hosted version?**
A: No. Coffer is self-hosted by design. Your transactions never touch our servers.

**Q: How fast is settlement?**
A: EVM: ~2 seconds (one RPC round-trip). Solana/TRON: instant (verification only, settlement is buyer-side).

**Q: What happens if my RPC goes down?**
A: The facilitator router automatically falls back to the next healthy facilitator. If all fail, payments are rejected (fail-closed).

**Q: Can I use this with AI agents?**
A: Yes. Coffer includes a native MCP server with paid tool surface. Any MCP-compatible agent can discover and pay for your tools.

**Q: Is it audited?**
A: Not yet. See [SECURITY.md](SECURITY.md) for the current audit status and our vulnerability reporting process.

---

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

1. Fork and create a feature branch
2. Install dev deps: `pip install "x402-coffer[dev]"`
3. Follow conventional commits (`feat:`, `fix:`, `docs:`)
4. `ruff check . && mypy coffer && pytest` must pass
5. Open a PR

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Rug Munch Media LLC.

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://rugmunch.io">Rug Munch Media LLC</a> — sovereign payments for the agentic economy.</sub>
</p>
