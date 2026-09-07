# Security Model

Coffer is engineered for high-stakes production environments with a **fail-closed** philosophy: when in doubt, deny. This document describes the guarantees and their boundaries.

## Fail-Closed by Default

- **No trusted client input.** Client-provided amounts, prices, and identities are never accepted at face value. All amounts are validated against authoritative server-side values before any chain contact.
- **Deny on uncertainty.** If the chain cannot be reached, the payment cannot be proven, or state is ambiguous, access is denied. Coffer never grants access on an unverified payment.
- **Fail-fast configuration.** A misconfigured merchant wallet or missing signing key aborts at startup with a clear error — never silently at request time.

## Signing Keys

- Self-facilitator signing keys come from `X402_SELF_FACILITATOR_KEY` (env) or, preferably, the secret file named by `X402_SELF_FACILITATOR_KEY_FILE`. Secret files are preferred for deployments: they avoid key material in process environments and shell history.
- Keys must never be embedded in code, committed to git, or logged. Coffer's logging paths exclude key material.
- Payments are cryptographically bound to a specific **operation** — a signature authorized for one resource cannot be replayed against a different service.

## Replay Protection

Settlement is idempotent and replay-safe:

- On first settlement, Coffer issues an **`X-Payment-ID`** — a caller-bound, TTL'd token.
- Replays of the same payment short-circuit through the replay check and return the original receipt. The payment is never settled twice.
- Replay tokens are bound to the caller identity recorded at verify time; a token stolen from one caller is worthless to another.
- Reconciliation conflicts (a payment ID already reconciled) return an RFC 7807 `409 Conflict` and never re-contact a facilitator.

## Rate Limiting and Spending Policy

Two independent controls run before any chain contact:

- **Request caps** (per minute/day) limit how often a caller can attempt payments.
- **Value caps** (per window) limit the total value a caller can move.

Both denials return structured RFC 7807 `429` problems with a `retry_after` hint — never bare error strings.

## Billing Ledger Integrity

- The billing surface (`/v1/billing/history`, `/v1/billing/balance`) is attributed exclusively to the verified payment's recorded caller key. A client-supplied `X-User-Id` header is never trusted — trusting it would turn the ledger into an IDOR oracle for other customers' data.
- Ledger records are persisted as JSONL with high durability (fsync) to survive crashes.

## Additional Hardening

- **SSRF guard** — strict validation of all callback and webhook URLs to prevent server-side request forgery.
- **HMAC webhooks** — payment event notifications are signed; receivers can verify authenticity.
- **Thread-safe state** — internal verification state uses robust locking to prevent race conditions under high concurrency.
- **fsync durability** — payment records are flushed to durable storage before acknowledgment.

## Known Boundaries

- Coffer has **no first-party API-key auth**. Caller identity is the best-effort `caller_key` (client IP / API key subject recorded at verify time). Deployments needing strong customer identity should front Coffer with an auth proxy and terminate identity there.
- The WSGI gate validates replay tokens only (no on-chain calls); full verification requires the ASGI path.

See [x402-spec-compliance.md](x402-spec-compliance.md) for the protocol flow and [integration.md](integration.md) for deployment wiring.
