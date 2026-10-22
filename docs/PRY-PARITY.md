# Pry ↔ Coffer x402 parity

> Generated 2026-10-23 against `pryscraper@main` (`src/pry/payments/x402*`) and
> `x402-coffer@main` (`coffer/_engine`). Pry is the reference: it is the more
> advanced implementation. Behaviour is ported through Coffer's own seams —
> no Pry code is copied.

## Feature matrix

| capability | pry | coffer (before) | gap |
|---|---|---|---|
| 402 challenge `PAYMENT-REQUIRED: <b64 JSON>` | yes — `x402_middleware/enforcement.py` L70-71 | yes — `_engine/response_handler.py` L91 | none |
| `PAYMENT-SIGNATURE` ingest (v2) | yes — `enforcement.py` L146 | **no** — `_engine/middleware.py` L33-35 `TODO(v2)` | **ported** → `payment_headers.decode_payment_header` |
| legacy `X-PAYMENT` ingest | no | no | **ported** (alias, lower precedence) |
| receipt header `PAYMENT-RESPONSE` | yes — `enforcement.py` L71 | yes — `settle_handler.py` L88 | none |
| receipt alias `X-PAYMENT-RESPONSE` | no | **no** | **ported** — `settlement_header_pairs` dual-emits one identical value |
| EIP-3009 `transferWithAuthorization` | yes — `self_facilitator/evm_verify.py` | yes — `_engine/self_facilitator.py` L123-300 | none |
| facilitator verify/settle with retries | yes — `facilitator.py`, `facilitator_settle.py` | yes — `settle_handler.py` L101-111 (3 attempts, honours `Retry-After`, capped 30 s) | none |
| settlement idempotency | durable gate: `settlement_gate.py` (`is_settled` over memory+JSONL+DB) + DB UNIQUE (`ledger_store.py`) | `_settled_txs` in memory + `PaymentStore.record_settle_outcome` (Redis when configured) | **partial** — see Remaining gaps #2 |
| per-route pricing | yes — `scheme/pricing/*` | yes — `_engine/pricing.py` (`X402_PRICING`, `resolve_price_usd`) | none |
| per-key budgets / quotas | yes — `handler_authorize.py`, `scheme/pricing/manifest.py` | velocity only (`ratelimit.py`), per-caller spend only (`spending.py`) — **no per-key money budget** | **ported** → `budgets.py` |
| usage ledger | Postgres — `ledger_store.py`, `handler_state_ledger.py` | in-memory `UsageTracker` + `settlements.jsonl` (receipt-shaped) | **ported** → `usage_ledger.py` (durable JSONL, keyed) |
| replay / nonce store | yes — `x402/handler_state*.py`, `x402_middleware/internal_replay_store.py` | yes — `replay.py` (Redis SETNX, in-memory fallback) | none |
| refund / void semantics | `void_payment` + ledger settlement reversal | `void_payment` (`verify_handler.py` L245); no ledger reversal row | **partial** — released reservations only (see #3) |
| server attestation of the challenge | yes — `scheme/attestation.py`, `scheme/digest.py` (keccak256 ECDSA, freshness window) | **none** — challenge body unsigned | **ported** → `attestation.py` |
| multi-network `exact` scheme | Base/Eth/Arb/Op/Polygon/Solana/TRON | same (`assets.py`) + Base Sepolia / Eth Sepolia | none |
| batch settlement | yes — `x402/batch/*` | yes — `_engine/batch.py` | none |
| metrics | yes — `x402/metrics.py` | yes — `_engine/observability.py` | none |

## What was ported

| file | behaviour | Pry reference |
|---|---|---|
| `coffer/_engine/payment_headers.py` | v2 + legacy payment-header decode, `PAYMENT-REQUIRED` encode, dual receipt headers | `x402_middleware/enforcement.py` L70-71, L146 |
| `coffer/_engine/attestation.py` | domain-separated keccak digest, per-accept ECDSA signature, freshness/clock-skew window, recoverable signer | `scheme/digest.py` L38-75, `scheme/attestation.py` |
| `coffer/_engine/budgets.py` | per-key money budget with atomic reserve → settle / release, per-payment cap, `retry_after` | `x402/handler_authorize.py`, `scheme/pricing/manifest.py` |
| `coffer/_engine/usage_ledger.py` | append-only fsynced JSONL ledger keyed by API key + summary for budget reconciliation | `x402/ledger_store.py`, `handler_state_ledger.py` |

## Wiring (second commit)

One implementation on every path — no second code path, no `TODO(v2)`:

```
X402Middleware / require_payment (adapters.py)
        -> gate.authorize_payment(headers, policy)
             -> gate_settle.pay_with_settler
                  attestation check  (attestation.verify_payment_required)
                  budget reserve     (budgets.check_and_reserve)
                  settler            (verify -> settle, injected)
                  budget settle/release + one usage_ledger row
        -> 2xx : payment_headers.settlement_header_pairs  (both names, one value)
        -> 402 : challenge.build_attested_challenge       (attested, reason, Retry-After)
```

- `middleware.py`: `TODO(v2)` removed; documents the one gate + one builder.
- `adapters.py`: `X402Middleware` and the async `require_payment` call the
  gate; the ASGI `send` is wrapped so a 2xx carries the receipt headers.
- `response_handler.py`: `create_payment_required` attests the body before
  encoding; new `settlement_headers(receipt)` returns both header names.
- Fail-closed budgets: reservation precedes the settler, release on refusal /
  exception / missing settler, `Retry-After` on exhaustion.
- Sync `require_payment` handlers keep the legacy `X-Payment-ID` check only —
  a synchronous call cannot await a settler, and faking it would serve unpaid.

## Verification

- `tests/test_pry_parity.py` — 12 tests: header decode/reject, dual receipt echo,
  attestation sign/verify/tamper/expiry, budget reserve-settle-release + ceilings,
  ledger append/read/summary, and verify→settle→replay→underpay through
  `FacilitatorRouter` + `X402Handler`.
- `tests/facilitator_fake.py` — local facilitator double: real EIP-3009 digest +
  `eth_keys` recovery, nonce burn, **no chain broadcast**.
- `tests/test_gate_wiring.py` — 12 tests through the real ASGI middleware:
  attested challenge, receipt echo on 200, release-on-refusal, `Retry-After`
  on exhaustion, missing settler, settler exception, tampered attestation,
  legacy `X-Payment-ID`, async decorator, malformed header, amount boundary.
- `scripts/prove_parity.py` — transcript through the WIRED middleware:
  402 (attested, `reason=payment_required`) → 200 + both receipt headers →
  replay 402 (`authorization_already_used`) → underpaid 402
  (`insufficient_amount`) → ledger `{settled: 1, released: 2}`.

## Remaining gaps

1. **Attestation wire compatibility** — Coffer signs its own canonical tuple
   (`coffer-x402/attestation/v1`). A signature produced by Pry will not verify
   here until the canonical list and domain are aligned. Behaviour parity only.
2. **Durable idempotency** — Coffer's `_settled_txs` is per-process; a restart
   with no Redis can re-attempt a settle that already landed. Pry's
   `settlement_gate.is_settled` reads memory + JSONL + DB. Recommended next:
   wire `usage_ledger.read_usage` + `PaymentStore` into a single `is_settled()`
   gate.
3. **Refund/reversal** — `void_payment` frees the gate but writes no negative
   ledger row, so per-key spend is not corrected. Pry records settlement
   reversal. Needs a `refund` outcome + negative amount in `usage_ledger`.
4. **Settle refusal reasons (partial)** — the gate now propagates the reason
   captured at *verify* time (`insufficient_amount`,
   `authorization_already_used`) into the 402 body. A refusal that only
   surfaces during *settle* is still collapsed by `settle_self_hosted_evm`
   into "self-hosted settle unavailable"; fixing that needs a typed result on
   the settle path.
5. **Custom facilitators are not consulted on settle** —
   `register_facilitator` lands in `_custom_facilitators`, read by verify but
   not by `settle_self_hosted_evm` (which uses `_impl_for` /
   `_get_self_facilitator`). Registration therefore implies a verify/settle
   asymmetry.
6. **Ledger backend** — JSONL, not Postgres. Fine for single-node; a DB
   repository (with a UNIQUE settlement key) is needed for multi-writer.
7. **Wire-in — closed.** The middleware, decorator and `X402Handler` share one
   gate and one challenge builder (see "Wiring" above). Remaining: the
   `/v1/x402/verify` HTTP route still records payments through
   `PaymentStore` rather than the gate, so a client that pays via that route
   and then presents `PAYMENT-SIGNATURE` is verified twice (idempotent, but
   two ledger rows are possible).
