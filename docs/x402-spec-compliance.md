# x402 Spec Compliance

How Coffer implements the [x402 protocol](https://www.x402.org/) — the quote → verify → settle flow that turns any HTTP resource into a pay-per-request endpoint.

## The x402 Flow

```text
Client                          Resource Server (+ Coffer)
  │                                     │
  │ 1. GET /resource                    │
  ├────────────────────────────────────►│
  │                                     │
  │ 2. HTTP 402 Payment Required        │
  │    PAYMENT-REQUIRED: <payload>      │  ← quote: price, network, asset, payTo
  │◄────────────────────────────────────┤
  │                                     │
  │ 3. Client pays on-chain             │
  │                                     │
  │ 4. GET /resource (retry)            │
  │    PAYMENT-SIGNATURE: <payload>     │
  ├────────────────────────────────────►│
  │                                     │
  │ 5. Verify + settle (idempotent)     │
  │                                     │
  │ 6. HTTP 200 OK                      │
  │    PAYMENT-RESPONSE: <receipt>      │
  │    X-Payment-ID: <verified id>      │
  │◄────────────────────────────────────┤
```

## Step by Step

### 1. Quote (HTTP 402)

When a client hits a protected route without payment, Coffer's middleware intercepts the request and returns `402 Payment Required` with a machine-readable `PAYMENT-REQUIRED` header payload containing:

- The price and asset (e.g., 0.001 USDC)
- The target network (CAIP-2 identifier, e.g., `eip155:8453`)
- The pay-to address (the merchant's receiving wallet)
- Operation binding — the payment is cryptographically bound to the specific operation requested

### 2. Verify

The client pays on-chain and retries the request with a payment signature. Coffer verifies the payment against the chain before granting access:

| Chain | Verification |
| :--- | :--- |
| EVM (Base, Ethereum, Arbitrum, Optimism, Polygon) | EIP-3009 `transferWithAuthorization` and EIP-7702 |
| Solana | SPL `transferChecked` on-chain verification with payer binding |
| TRON | TRC-20 transfer verification |

Verification is **fail-closed**: if the chain cannot be reached or the payment cannot be proven, access is denied. Client-provided amounts are never trusted — they are validated against authoritative server-side values.

### 3. Settle

Settlement is **idempotent**. Coffer issues a caller-bound, TTL'd `X-Payment-ID` on first settlement; any replay of the same payment short-circuits through the replay check and returns the original receipt instead of double-spending. The settlement is appended to the JSONL billing ledger (`settlements.jsonl`, `payments.jsonl`).

## Protocol Guarantees

- **RFC 7807 errors** — all denials (rate limit, spending policy, replay, reconciliation conflict) are structured `application/problem+json` responses with machine-readable problem types.
- **Reconciliation conflict handling** — if a payment ID is already reconciled, Coffer returns an RFC 7807 `409 Conflict` and never contacts a facilitator again.
- **Operation gating** — a payment authorized for one operation cannot be reused for a different resource or service.
- **Replay protection** — combines TTL expiry and caller binding so a signature cannot be reused by a different caller or after expiry.

## Deviations and Extensions

- Coffer adds a **billing ledger surface** (`/v1/billing/history`, `/v1/billing/balance`) on top of the base x402 spec, attributed to the verified caller key.
- Coffer supports **self-facilitator mode**, where the paying agent signs payments locally via `X402_SELF_FACILITATOR_KEY(_FILE)` instead of relying on an external facilitator.
- Coffer can route to an **external facilitator** via `X402_FACILITATOR_URL` when self-settlement is not desired.

See [integration.md](integration.md) to wire a resource server, and [security-model.md](security-model.md) for the threat model behind these guarantees.
