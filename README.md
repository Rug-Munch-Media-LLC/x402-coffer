# Coffer — x402 Multichain Payment Facilitator

> Self-hosted, non-custodial x402 payment facilitator: quote, verify and settle crypto micropayments for any HTTP API.

**License:** [MIT](LICENSE) · **Canonical:** https://git.rugmunch.io/RugMunchMedia/x402-coffer
**Mirrors:** [GitHub](https://github.com/Rug-Munch-Media-LLC) · [GitLab](https://gitlab.com/rug-munch-media-group) · [Codeberg](https://codeberg.org/RugMunchMedia)

<p align="center">
  <strong>The open-source facilitator for x402 payments — quote, verify, settle across EVM, Solana, and more.</strong><br/>
  Coffer is a self-hosted payment facilitator for the x402 protocol: it turns any HTTP resource into a
  pay-per-request endpoint using crypto micropayments. When a client hits a protected route, Coffer
  returns an <code>HTTP 402</code> quote, verifies the client's on-chain payment, and settles —
  non-custodially, on your own infrastructure. Built for AI agents, MCP servers, and APIs selling
  data or work.
</p>

<p align="center">
  <a href="https://git.rugmunch.io/RugMunchMedia/x402-coffer/actions"><img src="https://img.shields.io/badge/build-passing-brightgreen" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue" /></a>
  <a href="https://www.x402.org/"><img src="https://img.shields.io/badge/x402-v2%20spec-2ea44f" /></a>
</p>

<p align="center"><strong>Status: Beta</strong> — the core quote → verify → settle flow is live; TRON and the <code>upto</code> / batch-settlement schemes are scaffolded.</p>

---

## What Coffer Does

Coffer implements the x402 payment flow end to end: **quote → verify → settle**.

1. **Quote** — a client requests a protected resource; Coffer answers with `HTTP 402 Payment Required` and a machine-readable payment payload (price, network, asset, pay-to address).
2. **Verify** — the client pays on-chain and retries with a payment signature; Coffer validates it against the chain (EIP-3009/EIP-7702 on EVM, SPL `transferChecked` on Solana, TRC-20 on TRON).
3. **Settle** — Coffer settles idempotently, issues a replayable `X-Payment-ID`, and returns the resource with a `PAYMENT-RESPONSE` receipt.

### Current Capabilities

- **RFC 7807 errors** — every denial (rate limit, spending policy, replay, reconciliation conflict) is a structured `application/problem+json` response, never a bare string.
- **Idempotent settle with replay check** — settlement is keyed by payment ID; replays short-circuit via `X-Payment-ID` (caller-bound, TTL'd) so a signature can never be spent twice.
- **Rate limiting + spending policy** — per-caller request caps (per minute/day) and value caps (per window), enforced before any chain contact.
- **Billing ledger** — JSONL audit ledger (`settlements.jsonl`, `payments.jsonl`) with `/v1/billing/history` and `/v1/billing/balance` endpoints, attributed to the verified caller key only.
- **Merchant and payer modes** — run Coffer as the resource server's facilitator (merchant side) or as a self-facilitator that signs payments on behalf of an agent (payer side).
- **Self-facilitator signing** — point an agent at Coffer with `X402_SELF_FACILITATOR_KEY` (or `X402_SELF_FACILITATOR_KEY_FILE`) and it signs and routes payments without any external custodian.
- **`/health`** — liveness endpoint for orchestrators and load balancers.

## Who It's For

- **MCP servers** selling tool calls to agents — one decorator turns any tool into a paid endpoint.
- **API providers** monetizing data endpoints with crypto micropayments instead of API keys and invoices.
- **Autonomous agents** that need to *pay* other agents — Coffer's self-facilitator mode signs payments so agents can consume paid x402 resources.

## Architecture

```text
 ┌──────────────────────┐         ┌─────────────────────────────┐
 │  Actors              │         │  Coffer (self-hosted)       │
 │  ─ AI agent / MCP    │  402    │  ─────────────────────────  │
 │    client            │◄────────┤  quote → verify → settle    │
 │  ─ Resource server   │  pay    │  rate limit + spending      │
 │    (your API)        │────────►│  replay check + ledger      │
 └──────────────────────┘  retry  └──────────┬──────────────────┘
                                             │ verify / settle
                          ┌──────────────────┼──────────────────┐
                          ▼                  ▼                  ▼
                    ┌───────────┐     ┌───────────┐      ┌───────────┐
                    │ EVM       │     │ Solana    │      │ TRON      │
                    │ Base, ETH,│     │ SPL       │      │ TRC-20    │
                    │ Arbitrum, │     │ transfer  │      │ USDT/TRX  │
                    │ OP, Poly  │     │ Checked   │      │           │
                    └───────────┘     └───────────┘      └───────────┘
```

## Quick Start

```bash
pip install x402-coffer
```

### Merchant side (resource server)

```bash
export X402_WALLET="0xYourEVMAddress"
# Solana: export X402_WALLET_SOLANA="YourSolanaAddress"
# TRON:   export X402_WALLET_TRON="YourTronAddress"
```

```python
from coffer import require_payment


@app.get("/premium-tool")
@require_payment(asset="USDC", network="base", amount=0.001, operation="tool_use")
async def premium_endpoint(request):
    return {"status": "success", "data": "Access granted!"}
```

### Payer side (agent / consumer)

Point your client at a facilitator — Coffer's own, or any external one:

```bash
export X402_FACILITATOR_URL="https://your-coffer.example.com"
```

Or let an agent sign its own payments via the self-facilitator:

```bash
export X402_SELF_FACILITATOR_KEY="<signing key>"        # or:
export X402_SELF_FACILITATOR_KEY_FILE="/path/to/key"    # preferred for deployments
```

See [docs/integration.md](docs/integration.md) for full resource-server integration, including real consumer examples from `rmi-mcp-x402` and `walletpress`.

## Documentation

- [x402 Spec Compliance](docs/x402-spec-compliance.md) — how Coffer implements the x402 protocol flow
- [Integration Guide](docs/integration.md) — wiring a resource server or agent to Coffer
- [Security Model](docs/security-model.md) — fail-closed design, signing keys, replay protection
- [Architecture Deep Dive](ARCHITECTURE.md)
- [Deployment](docs/COFFER-DEPLOY.md) · [FAQ](docs/FAQ.md) · [Security Policy](SECURITY.md)

## Networks

| Network | CAIP-2 | Supported Assets | Verification Method |
| :--- | :--- | :--- | :--- |
| **Base** | `eip155:8453` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Ethereum** | `eip155:1` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Arbitrum** | `eip155:42161` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Optimism** | `eip155:10` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Polygon** | `eip155:137` | USDC, USDT, POL | EIP-7702 / EIP-3009 |
| **Solana** | `solana:mainnet` | USDC, USDT, SOL | SPL RPC Verification |
| **TRON** | `tron:mainnet` | USDT, TRX | TRC-20 Grid Verification |

## Configuration

Configuration is managed via environment variables with the `X402_` prefix.

| Variable | Description |
| :--- | :--- |
| `X402_WALLET` | Primary receiving EVM address. |
| `X402_NETWORK` | Default network (e.g., `base`, `ethereum`, `solana`). |
| `X402_FACILITATOR_URL` | Facilitator endpoint for consumers/agents (optional; defaults to an external facilitator). |
| `X402_SELF_FACILITATOR_KEY` | Signing key for self-facilitator (payer) mode. |
| `X402_SELF_FACILITATOR_KEY_FILE` | Secret file holding the signing key — preferred over the env var. |
| `REDIS_URL` | Connection string for the payment nonce/replay store. |
| `X402_OFFLINE_MODE` | Enable for testing without real on-chain verification. |

## Why Coffer

- **Open source (MIT) and self-hosted** — you own the payment facilitator; no third party sees your volume or customers.
- **Non-custodial** — funds move directly from payer to merchant via on-chain transfers.
- **Multichain** — 7 networks and 25+ assets out of the box.
- **Agent-native** — MCP server included; agents discover and pay for tools automatically.
- **No vendor lock-in** — route to external facilitators or settle entirely on your own.

## Links

- [Documentation](https://docs.rugmunch.io/x402-coffer)
- [x402 specification](https://www.x402.org/)

---

<p align="center">
  <sub>Built by <a href="https://rugmunch.io">Rug Munch Media LLC</a> — open-source payments for the agentic economy.</sub>
</p>
