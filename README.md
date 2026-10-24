# x402-coffer

An x402 facilitator: it issues payment challenges, verifies the payer's signature
and settlement, and records receipts so the same payment cannot be spent twice.
Point a metered API at it and stop writing payment code per service.

**Status:** beta · **Licence:** MIT, see [LICENSE](LICENSE).

Canonical: https://git.rugmunch.io/RugMunchMedia/x402-coffer

## Quickstart

Python 3.11+, Redis, an RPC endpoint, and a receiving address.

```bash
git clone https://git.rugmunch.io/RugMunchMedia/x402-coffer.git
cd x402-coffer
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env      # X402_WALLET, X402_SELF_FACILITATE, REDIS_URL
uvicorn --factory coffer._engine.config:create_app --port 8001
```

Compose runs the same service with its healthcheck:

```bash
docker compose up -d
curl -s localhost:8001/v1/x402/health
```

A metered request without a payment header returns 402 with the accepted
schemes and prices — that is a successful call, not an error.

## What it does

- **Challenge and verify.** Builds the 402 body, checks the payment payload
  against the declared scheme and network, and rejects mismatches before any
  handler runs.
- **Settlement.** Submits or verifies the transfer on EVM or Solana, depending on
  the configured network.
- **Replay protection.** Receipts are keyed in Redis; a repeated header is
  refused. `tests/test_replay_redis.py` is the case that matters.
- **Tool catalogue.** `/get_token_risk`, `/get_wallet_analysis`, `/get_news` and
  siblings are the priced endpoints; each has a declared price and a declared
  response shape.
- **Observability.** Request, verification and settlement events are emitted as
  structured events, not free-text logs.

## How it is put together

```
coffer/
  _engine/config.py     app factory and settings (uvicorn --factory target)
  routes                x402 challenge, verify, tool endpoints
  verification          scheme/network/amount checks
  receipts              Redis-backed replay store
coffer_client/          client helper for payers
vendor/                 pinned external x402 pieces
```

## Configuration

| Variable | Purpose |
|---|---|
| `X402_WALLET` | generic receiving address, used when a chain-specific one is unset |
| `X402_WALLET_BASE`, `X402_WALLET_ETH`, `X402_WALLET_ARBITRUM`, `X402_WALLET_OPTIMISM`, `X402_WALLET_POLYGON`, `X402_WALLET_SOLANA`, `X402_WALLET_TRON` | per-network payee addresses |
| `X402_SELF_FACILITATE` | settle locally instead of asking an upstream facilitator |
| `X402_SELF_FACILITATOR_KEY`, `X402_SELF_FACILITATOR_RPC` | signer and node for local settlement |
| `X402_EIP7702_RPC_8453` | Base RPC used for delegated-account payments |
| `REDIS_URL` | receipt store; without it replay protection is disabled |

## Known limits

- A single process with a single Redis. It is correct, not highly available;
  Redis loss means the replay window reopens.
- Prices are declared in code and configuration for the bundled catalogue. Wiring
  your own endpoints means adding route entries, not a config file alone.
- No fiat on-ramp. Payers must already hold the asset on the configured network.
- Testnet first. Mainnet settlement is a deliberate, manual configuration change.

## Licence

MIT.
