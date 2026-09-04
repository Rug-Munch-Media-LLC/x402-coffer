# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Competition-Sourced Features (2026-09)
- **Pre-settle + post-settle hooks** — register callbacks before/after settlement (from JS @x402/server)
- **Weighted random facilitator selection** — prevents thundering herd on best facilitator (from Coinbase x402-py)
- **Latency-aware health scoring** — health score factors average response time (from Coinbase x402-py)
- **Exponential backoff retry** — 3 attempts with 0.5*2^attempt delay on transient failures (from Coinbase x402-py)
- **Typed error hierarchy** — VerifyError, SettleError, FacilitatorTimeoutError with strict variants (from JS @x402/server)
- **Dynamic payment scheme negotiation** — select best scheme based on client capabilities (from Coinbase x402-py)
- **Auth-capture scheme** — authorize/capture/void flow for rental/subscription models (from JS @x402/server)
- **Extension mutation policy** — validate scheme extensions can't weaken security (from JS @x402/server)
- **EVM RPC fallback chain** — 2 fallback RPCs per network + retry (from Coinbase x402-py)
- **Solana payer binding** — validate on-chain authority matches expected payer (from pry parity)
- **Health-score-based routing** — facilitators sorted by successes - failures*2 (from Coinbase x402-py)



*Note: Coffer was born from pryscraper's payment system.*

### Security
- fail-closed pricing
- operation gating
- settlement idempotency
- fsync
- thread lock
- facilitator TTL
- pending_auths cleanup
- network validation
- TRON replay
- webhook HMAC
- SSRF guard
- redirect disable
- empty-secret refuse
- dead-letter redaction
- TRON contract filter
- atomic compare
- caller-binding
- batch caller_key

### Performance
- rate limit degraded cap
- LRU
- lazy loading
- store fsync
- batch expiry
- import fix

### Resilience
- EVM RPC fallback
- Solana payer binding
- health-score routing

## [Unreleased]

### Lifecycle Hooks & Routing (2026-09)
- **Retry-After-aware exponential backoff** — parses HTTP Retry-After header, 1s→30s cap (from JS @x402/server)
- **6-point lifecycle hooks** — pre/post/onFailure × verify/settle with abort + recovery semantics (from JS @x402/server)
- **register_facilitator plugin point** — register custom facilitator implementations per-network (from Coinbase x402-py)
- **Pre-existing mypy/ruff errors fixed** — handler.py, batch.py, scheme.py import fixes
- **Extension mutation policy tests** — validate_scheme_extension test coverage
- **Auth-capture tests** — authorize/capture/void flow test coverage
- **Typed error tests** — VerifyError, SettleError, FacilitatorTimeoutError test coverage

### Batch Channels & Escrow (2026-09)
- **On-chain batch settlement channels** — BatchChannel with voucher/claim/refund model, max_claims_per_batch=100, channel_id binding (from JS @x402/batch-settlement)
- **Auth-capture escrow EIP-712 PaymentInfo** — pre-approval/authorization/refund expiry windows, min/max fee bounds, capture_authorizer (from JS @x402/auth-capture)
- **BatchChannel tests** — create/claim/refund/overflow/post-refund coverage
- **PaymentInfo tests** — expiry/refund/fee validation coverage
