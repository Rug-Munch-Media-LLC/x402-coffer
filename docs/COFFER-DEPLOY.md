# Coffer Deployment Guide

> **Coffer** is the self-hosted x402 payment facilitator. This guide walks you
> through deploying it with Docker Compose, configuring receiving wallets per
> chain, choosing self-hosted vs external facilitation, scaling to multiple
> instances, and hardening before mainnet.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Configuration](#2-configuration)
3. [Wallet Setup](#3-wallet-setup)
4. [Self-Facilitation vs External Facilitator](#4-self-facilitation-vs-external-facilitator)
5. [Multi-Instance Deployment](#5-multi-instance-deployment)
6. [Health Checks and Monitoring](#6-health-checks-and-monitoring)
7. [Security Checklist Before Mainnet](#7-security-checklist-before-mainnet)

---

## 1. Quick Start

The repo ships a `Dockerfile` and `docker-compose.yml`. The image runs the
`/v1/x402` HTTP surface via uvicorn as a non-root user.

### 1.1 Configure your wallet

Copy the example env file and set at least one receiving wallet. **Coffer fails
closed at boot if no wallet is configured** — it will refuse to start rather
than serve an unconfigured 402.

```bash
cp .env.example .env
# Edit .env — set at least one of:
#   X402_WALLET=0x...            (EVM, Base USDC by default)
#   X402_WALLET_SOLANA=...       (Solana base58)
#   X402_WALLET_TRON=T...        (TRON)
```

### 1.2 Build and start

```bash
docker compose up -d --build
```

### 1.3 Verify

```bash
# Health endpoint
curl http://localhost:8001/v1/x402/health

# Root metadata
curl http://localhost:8001/
```

The root endpoint reports `wallet_configured: true` and the list of supported
networks. The health endpoint reports verification/settlement status per chain.

### 1.4 Stop

```bash
docker compose down
```

---

## 2. Configuration

All configuration is via environment variables (the `X402_*` prefix). The
container reads them from `.env` through `docker-compose.yml`'s `env_file`.
The full reference lives in [`.env.example`](../.env.example).

### 2.1 Receiving wallets / networks

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_WALLET` | Canonical EVM receiving address (Base USDC by default) | — | Yes* |
| `X402_WALLET_BASE` | Base (`eip155:8453`) receiving address | `X402_WALLET` | No |
| `X402_WALLET_ETH` | Ethereum (`eip155:1`) receiving address | `X402_WALLET` | No |
| `X402_WALLET_ARBITRUM` | Arbitrum (`eip155:42161`) receiving address | `X402_WALLET` | No |
| `X402_WALLET_OPTIMISM` | Optimism (`eip155:10`) receiving address | `X402_WALLET` | No |
| `X402_WALLET_POLYGON` | Polygon (`eip155:137`) receiving address | `X402_WALLET` | No |
| `X402_WALLET_SOLANA` | Solana receiving address (base58) | — | No |
| `X402_WALLET_TRON` | TRON receiving address (`T…`) | — | No |

\* At least one receiving wallet overall is required (EVM, Solana, or TRON).

### 2.2 Self-hosted facilitation / settlement

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_SELF_FACILITATE` | Enable self-hosted settlement (EIP-3009 / SPL / TRC-20) | `1` (true) | No |
| `X402_SELF_FACILITATOR_KEY` | Private key of the settlement account (EVM) | — | If settling EVM |
| `X402_SELF_FACILITATOR_RPC` | JSON-RPC endpoint for EVM settlement | per-network RPC | No |
| `X402_EIP7702_RPC_8453` | Base JSON-RPC endpoint (EIP-7702 verify) | `https://mainnet.base.org` | No |
| `X402_EIP7702_RPC_1` | Ethereum JSON-RPC endpoint | `https://eth.llamarpc.com` | No |
| `X402_EIP7702_RPC_42161` | Arbitrum JSON-RPC endpoint | `https://arb1.arbitrum.io/rpc` | No |
| `X402_SOLANA_RPC` | Solana JSON-RPC endpoint (SPL verify) | public endpoint | No |
| `X402_OFFLINE_MODE` | Dev-only: skip on-chain verification | `0` (false) | No |
| `X402_NONCE_TTL` | Replay-nonce TTL in seconds | `86400` (24h) | No |
| `X402_REDIS_URL` | Redis URL for shared replay store | — | Multi-instance |

> **Note on EIP-7702 RPC names:** the env suffix is the **CAIP-2 chain id**
> (`8453`, `1`, `42161`), not a name like `BASE`. See
> `coffer/_engine/evm_verify.py`.

### 2.3 External facilitator routing

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_COINBASE_ENABLED` | Enable Coinbase facilitator | `1` (true) | No |
| `X402_COINBASE_VERIFY` | Coinbase verify URL | `https://x402.org/facilitator/verify` | No |
| `X402_COINBASE_SETTLE` | Coinbase settle URL | `https://x402.org/facilitator/settle` | No |
| `X402_COINBASE_PRIORITY` | Coinbase routing priority | `1` | No |
| `X402_COINBASE_API_KEY` | Coinbase API key | — | No |
| `X402_PAYAI_ENABLED` | Enable PayAI facilitator | `1` (true) | No |
| `X402_PAYAI_VERIFY` | PayAI verify URL | `https://facilitator.payai.network/verify` | No |
| `X402_PAYAI_SETTLE` | PayAI settle URL | `https://facilitator.payai.network/settle` | No |
| `X402_PAYAI_PRIORITY` | PayAI routing priority | `2` | No |
| `X402_CF_ENABLED` | Enable Cloudflare facilitator | `0` (false) | No |
| `X402_CF_VERIFY` | Cloudflare verify URL | — | No |
| `X402_CF_SETTLE` | Cloudflare settle URL | — | No |
| `X402_CF_PRIORITY` | Cloudflare routing priority | `3` | No |
| `X402_FACILITATOR_URL` | Default facilitator URL | `https://facilitator.payai.network` | No |

### 2.4 Protocol / expiry / validation tuning

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_DEFAULT_NETWORK` | Default network advertised (CAIP-2) | `eip155:8453` | No |
| `X402_TIMEOUT_SECONDS` | Outbound HTTP timeout (s) | `15` | No |
| `X402_PAYMENT_TTL` | Payment authorization TTL (s) | `3600` | No |
| `X402_EIP712_NAME` | EIP-712 domain name | `USD Coin` | No |
| `X402_EIP712_VERSION` | EIP-712 domain version | `2` | No |
| `X402_PRICING_JSON` | Server-side pricing table (JSON) | — | No |

### 2.5 Enforcement / burst (legacy routers)

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_burst_limit` | Burst rate limit | `5` | No |
| `X402_burst_window` | Burst window (s) | `60` | No |

### 2.6 Secrets / integrations

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `WALLET_VAULT_PASSWORD` | Wallet vault password | — | No |
| `RMI_MCP_API_KEY` | RMI MCP integration API key | — | No |

### 2.7 Operator UX

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `X402_HOST` | Bind host | `0.0.0.0` | No |
| `X402_PORT` | Bind port | `8001` | No |
| `X402_DEBUG` | Debug mode (dev only) | `0` (false) | No |

---

## 3. Wallet Setup

Coffer is **non-custodial**: funds move directly from buyer to seller. You
configure the **receiving addresses** (where payments land) per chain.

### 3.1 EVM (Base, Ethereum, Arbitrum, Optimism, Polygon)

Set the canonical EVM wallet, or a per-network override:

```bash
# Single EVM wallet for all EVM chains
X402_WALLET=0xYourEVMAddress

# Or per-network overrides
X402_WALLET_BASE=0xBaseAddress
X402_WALLET_ETH=0xEthAddress
X402_WALLET_ARBITRUM=0xArbitrumAddress
X402_WALLET_OPTIMISM=0xOptimismAddress
X402_WALLET_POLYGON=0xPolygonAddress
```

Each per-network value overrides `X402_WALLET` for that chain; unset networks
fall back to `X402_WALLET`.

### 3.2 Solana

```bash
X402_WALLET_SOLANA=YourSolanaBase58Address
```

### 3.3 TRON

```bash
X402_WALLET_TRON=TYourTronAddress
```

### 3.4 Self-facilitator settlement key (EVM)

If `X402_SELF_FACILITATE=1` and you settle EVM payments, set the private key of
the account that signs and broadcasts EIP-3009 settlement transactions:

```bash
X402_SELF_FACILITATOR_KEY=0xYourPrivateKey
```

> **Security:** this key must be funded with gas tokens on each EVM chain you
> settle. Keep it in a secret manager, never in git. See the
> [security checklist](#7-security-checklist-before-mainnet).

---

## 4. Self-Facilitation vs External Facilitator

Coffer routes verification/settlement through a health-aware
`FacilitatorRouter` with automatic fallback.

### 4.1 Self-hosted (default)

With `X402_SELF_FACILITATE=1` (default), Coffer verifies and settles on-chain
itself:

- **EVM** — EIP-3009 authorization + broadcast, or EIP-7702 receipt verify
- **Solana** — SPL RPC verification (`getTransaction` jsonParsed)
- **TRON** — TRC-20 Grid verification

**Pros:** sovereign, no third-party visibility, no dependency on external
facilitators. **Cons:** you run the RPC endpoints and fund the settlement key.

### 4.2 External facilitator

Set `X402_SELF_FACILITATE=0` and enable one or more external facilitators
(Coinbase, PayAI, Cloudflare). Coffer routes through them with health-aware
fallback and priority ordering.

**Pros:** no settlement key to manage, no RPC infrastructure. **Cons:** third
party sees transaction volume; dependency on their availability.

### 4.3 Hybrid

Keep self-facilitation on and enable external facilitators as fallbacks. The
router picks the healthiest path and falls back automatically.

---

## 5. Multi-Instance Deployment

The replay-nonce store defaults to an **in-memory** store, which is fine for a
single process. For multiple instances/workers behind a load balancer, you must
share the nonce store via Redis so a nonce used on one instance is rejected on
all others.

### 5.1 Add Redis

```yaml
# docker-compose.yml (add a redis service)
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]

  coffer:
    build: .
    ports:
      - "8001:8001"
    env_file:
      - path: .env
        required: false
    environment:
      X402_REDIS_URL: redis://redis:6379/0
    depends_on:
      - redis
    restart: unless-stopped
```

### 5.2 Scale

```bash
docker compose up -d --scale coffer=3
```

Place a load balancer (nginx, Traefik, cloud LB) in front. All instances share
the same Redis nonce store, so replay protection holds across the fleet.

> **Note:** the JSONL payment ledger (`store.py`) is local to each instance.
> For a shared ledger across instances, mount a shared volume or point the
> store at a shared filesystem.

---

## 6. Health Checks and Monitoring

### 6.1 Health endpoints

| Endpoint | Purpose |
| :--- | :--- |
| `/v1/x402/health` | Full health: version, facilitator, oracle, per-chain verification status, settlement stats |
| `/health` | Liveness |
| `/healthz` | x402 V2 liveness |
| `/readyz` | x402 V2 readiness |
| `/metrics` | Prometheus metrics |
| `/dashboard` | Read-only ops dashboard |

The Docker image includes a built-in `HEALTHCHECK` hitting
`/v1/x402/health` every 30s.

### 6.2 Prometheus metrics

| Metric | Type | Description |
| :--- | :--- | :--- |
| `x402_verify_total` | counter | Verification attempts (`network`, `asset`, `result`) |
| `x402_verify_duration` | histogram | Verification latency |
| `x402_settle_total` | counter | Settlement attempts |
| `x402_settle_duration` | histogram | Settlement latency |
| `x402_facilitator_health` | gauge | 1 if facilitator healthy, 0 if failing |
| `x402_replay_rejected_total` | counter | Replay attacks rejected |

Scrape `/metrics` with Prometheus and alert on:
- `x402_facilitator_health == 0` (facilitator down)
- `x402_replay_rejected_total` increasing (possible replay attack)
- `x402_settle_total{result="failed"}` increasing (settlement failures)

### 6.3 Structured logging

All verification attempts are structured-logged as JSON
(`event: "x402_verify"`, `payment_id`, `tx_hash`, `network`, `verified`,
`elapsed_ms`, …). Ship logs to your aggregator (Loki, ELK, etc.).

---

## 7. Security Checklist Before Mainnet

- [ ] **At least one receiving wallet set** — Coffer fails closed at boot if
      none is configured. Verify `wallet_configured: true` on `/`.
- [ ] **`X402_OFFLINE_MODE` is `0`** — never run offline mode in production
      (it marks everything paid without on-chain verification).
- [ ] **`X402_DEBUG` is `0`** — debug mode is for development only.
- [ ] **Settlement key secured** — `X402_SELF_FACILITATOR_KEY` and
      `WALLET_VAULT_PASSWORD` come from a secret manager (Docker secrets,
      gopass, Vault), never from git or plaintext `.env` committed to the repo.
- [ ] **Settlement key funded** — the EVM settlement account has gas on every
      chain you settle, or settlement fails with `INSUFFICIENT_GAS`.
- [ ] **RPC endpoints are trusted** — use your own or reputable public RPCs.
      A malicious RPC could return forged receipts.
- [ ] **Redis secured** — if using multi-instance, Redis is not exposed
      publicly and requires auth.
- [ ] **TLS in front** — terminate TLS at your reverse proxy / load balancer.
      Never serve the 402 surface over plain HTTP in production.
- [ ] **Rate limiting** — confirm burst limits (`X402_burst_limit` /
      `X402_burst_window`) are tuned for your traffic.
- [ ] **Webhook secrets configured** — if using webhooks, a secret must be set
      or delivery is refused (fail-closed).
- [ ] **Nonce TTL tuned** — `X402_NONCE_TTL` (default 24h) balances replay
      protection against memory/Redis growth.
- [ ] **Backups** — back up the JSONL payment ledger and any wallet vault.
- [ ] **Least privilege** — the container runs as non-root `coffer` user
      (already the case in the shipped Dockerfile).
- [ ] **Review SECURITY.md** — full threat model and mitigation strategies.

---

## See Also

- [`.env.example`](../.env.example) — full environment variable reference
- [ARCHITECTURE.md](../ARCHITECTURE.md) — design, payment flow, security architecture
- [SECURITY.md](../SECURITY.md) — security policy and threat model
- [README.md](../README.md) — product overview and quickstart
