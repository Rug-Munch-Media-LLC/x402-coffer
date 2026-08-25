# Coffer — the self-hosted x402 payment facilitator

Coffer is the self-hosted x402 payment facilitator: accept `402 Payment
Required` micropayments in one line of Python, on your own infrastructure —
your HTTP treasury, sovereign and open. No hosted facilitator, no vendor
lock-in, no hostage dollars.

[![x402 v1+v2](https://img.shields.io/badge/x402-v1%20%2B%20v2-blue)](#2026-spec-compliance)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![self-hosted](https://img.shields.io/badge/deploy-self--hosted-purple)](#why-coffer)
[![spec-compliant](https://img.shields.io/badge/conformance-canonical-brightgreen)](docs/x402-official-spec/)
[![package coffer](https://img.shields.io/badge/package-coffer%205.0.0-blueviolet)](#quickstart--one-line)


```python
import coffer

@coffer.require_payment(asset="USDC", network="base", amount=0.001)
async def paid_endpoint():
    return {"data": "you paid for this"}
```

That's it. That decorator is a spec-exact `HTTP 402` → verify → settle FastAPI
gate. And because **Coffer ships its own in-process facilitator**, the whole
rail — offer, verification, settlement, replay protection — runs inside your
process. You keep the keys, you keep the treasury, you keep the custodian.

---

## Why Coffer

**Sovereign payments.** Coffer is the OSS x402 *self*-facilitator. Every
settlement path defaults to **your own signature** (EIP-3009 / EIP-7702 on EVM,
SPL `transferChecked` on Solana). No third-party facilitator is required to
collect revenue — `X402_SELF_FACILITATE=1` flips you to pure on-chain
self-settlement. The treasury is *your* wallet, not a hosted ledger.

**Zero vendor lock-in.** Wire format is the open x402 standard; the facilitator
is swappable behind `FacilitatorRouter`; the facade (`coffer/`) is a tiny public
API. If a better facilitator exists tomorrow, you point `register_facilitator()`
at it and move on. Your payments are never held hostage to a service's uptime,
pricing, migration, or shutdown.

**Broad, honest payment surface.** Today Coffer advertises **[6 live networks ×
22 live EVM options] + [3 live SVM options on Solana] = 25 live
(network, asset) payment options** across EVM, Solana (+ ARB on Arbitrum, POL on
Polygon &mdash; and TRON scaffolded). Self-hostable OSS facilitators rarely ship
this many live pairs; we intend Coffer to be **among the broadest self-hostable
facilitators** in the ecosystem. Agents see every payable pair in a single
`accepts[]` and pick freely.

**AI-forward.** Agents don't open wallets in browsers — they pay over HTTP.
Coffer is **MCP-native**: it vendors `FastMCP`, gates your agent tools with the
same `@coffer.require_payment` decorator, and exposes a discoverable paid MCP
server. Any agent that speaks the x402 handshake can pay your API and get the
resource back. Payments for the machine economy, done by machines.

---

## Payment options

Canonical `(network, asset)` matrix, **CAIP-2** network ids, `exact` scheme with
atomic-unit amounts. Advertised additively via `build_multi_asset_accepts`, so a
client reads **every** payable pair and chooses.

### EVM — live (`eip155:*`, EIP-3009 / EIP-7702 / Permit2)

| Network (CAIP-2) | Chain | USDC | USDT | ETH | WETH | ARB | POL |
| ---------------- | ----- | :--: | :--: | :-: | :--: | :-: | :-: |
| `eip155:8453` | Base | ✅ | ✅ | ✅ | ✅ | — | — |
| `eip155:1` | Ethereum | ✅ | ✅ | ✅ | ✅ | — | — |
| `eip155:42161` | Arbitrum | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| `eip155:10` | Optimism | ✅ | ✅ | ✅ | ✅ | — | — |
| `eip155:137` | Polygon | ✅ | ✅ | ✅ | ✅ | — | ✅ |

> **22 live EVM options.**

### SVM — live (`solana:mainnet`, SPL `transferChecked`)

| Network (CAIP-2) | USDC | USDT | SOL |
| ---------------- | :--: | :--: | :-: |
| `solana:mainnet` | ✅ | ✅ | ✅ |

> **3 live SVM options.**

### TRON — scaffolded (`tron:mainnet`)

| Network (CAIP-2) | USDT | TRX | Status |
| ---------------- | :--: | :-: | ------ |
| `tron:mainnet` | ✅ | ✅ | `verified:false` — structure/price/asset validated; needs a facilitator impl |

**[6 live networks, 25 live (network, asset) options.]** [REVIEW]
(arithmetic) rows sum to 25, not the 24 previously advertised; verify the
`accepts[]` set matches before merge. Honest status is exposed on
`/v1/x402/health`: `evm=live`, `solana=live`, `tron=scaffolded:needs_facilitator`.

> [REVIEW] DAI and MATIC are priced in the oracle but **not yet** in the
> advertised `accepts[]` matrix. We document only the live/payable set above.

---

## Honesty section — what is and isn't live

We will not overclaim. This repo was fixed so that nothing is reported verified
unless it is. Concretely:

- **Scaffolded paths are never reported verified.** TRON, the `upto` scheme, and
  `batch-settlement` are structural work only: shape/price/asset are validated,
  and `/v1/x402/health` reports `tron=scaffolded:needs_facilitator` until a real
  facilitator impl is registered. `verified:false` is the honest answer.
- **Verifying is not settling.** EIP-7702 self-verify reads **on-chain receipts
  only**; Solana's SPL RPC verifies **on-chain receipts only**. Neither
  broadcasts a settlement. Returning `success:true` there would *fabricate* a
  settlement record, so both **fail closed** with `success:false,
  reason:"needs_facilitator … verify-only"`. See C1/C2 in
  [`docs/COFFER-CHANGELOG.md`](docs/COFFER-CHANGELOG.md).
- **A bare HTTP-200 is not proof of payment.** `M2` requires an explicit
  verification/settlement signal (`isValid`/`verified`/`settled` true, or a
  transaction reference) from a facilitator before reporting `success`. `H3`
  removes `success:true` alone as a verification gate.
- **Gas-sponsoring is not free gas.** Un-sponsored gas-sponsoring (EIP-2612 /
  ERC-20 approval) **never reports verified** and never fabricates a `tx_hash`;
  without a configured `sponsor_impl`, settlement raises `not implemented —
  needs_facilitator` (`H1`). When it does run: the **buyer pays payment gas; the
  sponsor fronts the approval gas only** (`M1`).
- **Nonces are atomic.** In-memory replay protection serializes check-then-add
  under an `asyncio.Lock` (`H2`); Redis `SETNX` remains canonical in production.

---

## Extensions — 2026 extension spec

| Extension | Status |
| --------- | ------ |
| **payment-identifier** — idempotent, caller-bound, TTL'd verified ids (replay by a different caller rejected) | ✅ implemented |
| **EIP-2612 gas-sponsoring** — gasless permit-style payments | ✅ implemented |
| **ERC-20 approval gas-sponsoring** — gasless ERC-20 approve payments | ✅ implemented |
| **`upto`** scheme — authorize up to a maximum, settlement flexible | 🔜 scaffolded (scheme landed in official SDK; not reported verified) |
| **batch-settlement** — deposit + escrow-style multi-settle (before/after-handler phases) | 🔜 scaffolded |

---

## Comparison — Coffer vs the facilitator field

Source: [`docs/x402-official-spec/dev-tools__facilitators.md`](docs/x402-official-spec/dev-tools__facilitators.md)
(the official facilitator catalog). Our distinctive axis: **self-hosting +
open source + breadth at once.** [REVIEW] Rival-prowess cells below are curated
from the catalog; verify each cell against a cited upstream source before
publish (Coinbase/PayAI/Cloudflare settlement routing, Mogami Base-only,
Fireblocks vault coupling).

| Capability | **Coffer** | Coinbase (CDP) | PayAI | Cloudflare | Fireblocks | Mogami |
| ---------- | :---: | :---: | :---: | :---: | :---: | :---: |
| Open source (OSS) | ✅ MIT | ❌ | ❌ | ❌ | ✅ | ✅ |
| **Self-hosted** (no hosted dependency) | ✅ | ❌ | ❌ | ❌ | ⚠️ Fireblocks vault | ⚠️ Docker, Base-only |
| **Networks live** | **6** (EVM×5 + Solana) | Solana mostly | multi | — | multi-chain | Base only |
| **Live payment options** | **25** | USDC-centric | all tokens | — | — | USDC |
| Self-settle in-process (no facilitator) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| TRON | scaffolded | ❌ | — | ❌ | ❌ | ❌ |
| MCP payment wrapper | ✅ native | ❌ | ❌ | ❌ | ❌ | ❌ |

**Verdict:** on **self-hosting** we beat every hosted facilitator (Coinbase /
PayAI / Cloudflare route settlement through their service by design). On
**network breadth** we are among the broadest self-hostable OSS options (Mogami
is Base-only; Fireblocks is OSS but couples settlement to its hosted vault).
Coffer is OSS, self-hosted, and self-settling — a combination no single rival in
the catalog now provides. [REVIEW] "Among the broadest self-hostable" is a
position we assert, not one we've proven against a dated public audit; add a
citation or keep the hedge before publishing.

---

## Quickstart — one line

```bash
pip install coffer
```

```python
import coffer
from fastapi import FastAPI

app = FastAPI()

@app.get("/paid")
@coffer.require_payment(asset="USDC", network="base", amount=0.001)
async def paid_endpoint():
    return {"data": "paid in one line"}
```

Curl it unpaid → `402 Payment Required` with the full `PAYMENT-REQUIRED`
header (`accepts[]` built from your configured networks/assets). Pay via any
configured facilitator **or the built-in self-facilitator**, replay with
`X-Payment-ID`, get `200`.

Full runnable examples:
- [`examples/fastapi_paid_endpoint.py`](examples/fastapi_paid_endpoint.py) — minimal `@require_payment` route
- [`examples/mcp_server_paid.py`](examples/mcp_server_paid.py) — MCP server with a paid tool (client auto-pay bridge + server-side gate)
- [`examples/self_hosted_facilitator.py`](examples/self_hosted_facilitator.py) — stand up your own verify/settle, no Coinbase

---

## Facilitator routing

`FacilitatorRouter` picks a health-aware facilitator with automatic fallback —
**including your own in-process self-facilitator**.

| Mode | Setting | Verifies / settles |
| --- | --- | --- |
| **Self-hosted (default)** | `X402_SELF_FACILITATE=1` | your EIP-3009 / EIP-7702 (EVM) + SPL (Solana) |
| Coinbase / x402.org | `https://x402.org/facilitator` | hosted (optional) |
| PayAI | `https://facilitator.payai.network` | hosted (optional) |
| Cloudflare | env URLs | hosted (opt-in) |

Drop the hosted facilitators entirely and Coffer settles on-chain with **no
external dependency**.

---

## 2026 spec compliance

- **Protocol:** v1 **and** v2 wire formats (`PaymentRequired` / `PaymentPayload`
  / `SettlementResponse`). Exact header scheme: `HTTP 402` +
  `PAYMENT-REQUIRED: <base64 JSON>` + `X-Payment-Required: true` +
  `X-Payment-Options: x402`; replay via `X-Payment-ID`.
- **Schemes:** `exact` (EVM `eip3009` / `permit2` / `native` + SVM `spl`).
  `upto` / `batch-settlement` are scaffolded (not reported verified).
- **Extensions:** keys `payment-identifier`, `eip2612-gas-sponsoring`,
  `erc20-approval-gas-sponsoring`.
- **Vendored x402 spec:** the repo carries the official 2026 `docs/x402-official-spec/`
  (v1 + v2) and delegates verification/settlement through `x402-py`'s public API
  **or** the self-hosted verifier — fail-fast, no silent fallback.

> See [ARCHITECTURE.md](ARCHITECTURE.md) for the 402 flow and module layering,
> [CONTRIBUTING.md](CONTRIBUTING.md) to add a network or extension, and
> [`docs/COFFER-CHANGELOG.md`](docs/COFFER-CHANGELOG.md) for the build record.

---

## Scope

- x402 payment verification + settlement (facilitator & self-hosted)
- MCP server exposing a paid, discoverable tool surface
- 2026 extension support (idempotency, gas-sponsoring, schemes)

## Standards

Follows https://git.rugmunch.io/RugMunchMedia/standards · MIT License

## FAQ

See [docs/FAQ.md](docs/FAQ.md) for frequently asked questions.
