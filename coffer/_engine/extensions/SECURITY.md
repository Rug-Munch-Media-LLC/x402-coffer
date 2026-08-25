# SECURITY — x402 extensions + payment-verification core audit

Scope: `app/domain/x402/extensions/*` (EIP-2612 gas sponsoring, ERC-20 approval
gas sponsoring, idempotency, upto/batch stubs) + the payment-verification core
in `app/domain/x402/service.py` (FacilitatorRouter: nonce/replay, underpayment,
scaffold honesty). Findings as of 2026-08-21. Crypto implemented in
`gas_sponsoring.py`; audit findings below.

## EIP-2612 gas sponsoring — implemented crypto

Real EIP-712 typed-data verification, mirroring `x402/self_facilitator.py`'s
`ecrecover` pattern:

- **Domain separator**: `keccak(keccak("EIP712Domain(string name,string
  version,uint256 chainId,address verifyingContract)") || keccak(name) ||
  keccak(version) || uint256(chainId) || address(token))` — verifyingContract is
  the ERC-20 token, chainId parsed from the CAIP-2 network.
- **Permit struct hash**: `keccak(keccak("Permit(address owner,address
  spender,uint256 value,uint256 nonce,uint256 deadline)") || owner || spender ||
  value || nonce || deadline)`.
- **Digest**: `keccak(b"\x19\x01" + domainSeparator + structHash)` — verified
  byte-identical to `eth_account.sign_typed_data` / `encode_typed_data` output
  (recovered signer == `owner` under `eth_keys.PublicKey.recover_from_msg_hash`).
- **Honest status**: the sponsor path either delegates to a configured
  `sponsor_impl` (real sponsor key → its tx_hash) or reports
  `scaffolded:needs_sponsor_key`; `apply_gas_batch` raises `NotImplementedError`
  without a sponsor — **no fabricated tx_hash, no `verified:true`** for the
  un-executed on-chain step. `build_permit_calldata` emits real
  `permit(owner,spender,value,deadline,v,r,s)` ABI (`0xd505accf` selector, 7 words)
  only after local signature verification.

`[REVIEW]` — real sponsor key / external verification still required for the
on-chain half (see flags at bottom).

## Audit findings — nonce / replay (FacilitatorRouter.ReplayStore)

- **Redis SETNX atomicity: OK.** `SET key 1 EX ttl NX` is a single atomic Redis
  command — lock-free, safe against concurrent duplicate `verify` of the same
  tx_hash. `check_and_burn` correctly returns True (claimed) / False (replay).
- **flag — in-memory fallback**: when `REDIS_URL` is unset (or Redis not
  installed / `redis` import fails) `ReplayStore` silently falls back to a
  bounded in-process `set`. **NOT multi-instance safe** — two app replicas can
  each consume the same nonce and double-verify. The module already `[REVIEW]`s
  this, but production MUST configure `REDIS_URL`; the fallback should log a
  WARNING on every use (not just at construction) and ideally refuse to burn for
  replay-sensitive assets when Redis is absent. **flag — prod requirement.**

## Underpayment — atomic-unit comparison

- **OK.** `asset_to_atomic` uses `Decimal` + `math.ceil` of the USD/price ×
  `10**decimals`, and `validate_client_amount`/`self-facilitator` compare in
  **atomic units** (`client_atomic < required_atomic` → reject). Overpayment
  allowed; underpayment refused. The `-1e-9` epsilon in `asset_to_atomic` is a
  float-rounding fuzz-guard on the ceil, and can only *lower* the required
  amount by <1 wei, never raise it — not exploitable to underpay (attack would
  need the *server* to under-require, not the client). Static USD quotes are the
  oracle (flat stablecoins 1:1) — see flag.
- **flag — pricing oracle**: `_STATIC_FALLBACK_USD` is operator-set static.
  A stale/wrong stablecoin price only breaks correctness of the *required*
  amount; there is no oracle-manipulation surface from client input. Ensure
  operators audit `X402_<ASSET>_USD` overrides. Low risk.

## Scaffold honesty — TRON + upto/batch stubs

- **OK.** `VERIFICATION_STATUS` maps tron → `scaffolded:needs_facilitator`;
  `SCAFFOLDED_NETWORKS` includes `tron:mainnet`; the TRON path (`_scaffold_non_evm_verify`)
  never returns `verified:true`. `UpToScheme.settle` / `BatchSettlementScheme.claim_and_settle`
  `raise NotImplementedError` (never fabricated hashes).
- **fix — EIP-2612 branch never ran (found during audit).** `resolve_sponsor_flow`
  returned short labels (`"eip2612"`) while validation/extraction compared against
  the full extension key (`EIP2612_KEY == "eip2612GasSponsoring"`) and indexed
  `exts.get("eip2612")` (no such key). Result: the EIP-2612 path always fell into
  the ERC-20 branch, **never validating nor verifying the permit**. FIXED: flow
  resolution now returns full extension keys.
- **fix — nonce/deadline zero rejected.** Presence validation used truthiness, so
  a valid permit with `nonce == 0` (a fresh owner's EIP-2612 nonce IS 0) was
  rejected as "missing". FIXED: presence now means "key present and non-empty";
  numeric zero is accepted.
- **OK — ERC-20 approval path**: structure validated, `approve() to` must be
  the canonical Permit2; the raw signed-tx broadcast/`settle` is delegated to the
  sponsor and never verified locally (honest).

## Replay-store in-memory fallback

- **flag** (repeated above): `ReplayStore` and `PaymentIdentifierStore` both
  degrade to in-process memory without Redis — **not multi-instance safe**.
  Required-for-prod: `REDIS_URL` configured so tx_hash burn + payment-id
  idempotency are shared across replicas. The in-memory fallback must be treated
  as dev-only.

## CVSS / OWASP mapping (summary)

| Finding | Status | CVSS 3.1 | CWE |
|---|---|---|---|
| EIP-2612 permit never verified (branch bug) | fixed | 8.1 (forged permit accepted pre-fix) | CWE-347 |
| Nonce-zero rejected (valid permit DoS) | fixed | 6.5 | CWE-703 |
| Multi-instance replay fallback (no Redis) | flagged | 6.3 (replay/double-verify) | CWE-367 |
| Underpayment float drift | ok (Decimal/atomic) | n/a | CWE-681 (mitigated) |
| TRON/upto/batch scaffold honesty | ok | n/a | CWE-1104 |

OWASP: underpayment/nonce maps to A01/Broken Access Control & A09/Logging;
forged permit maps to A07/Identification & Auth failures.

## [REVIEW] flags — external verification / sponsor key needed

1. **Token EIP-712 domain**: `name`/`version` default to "USD Coin"/"1" (env
   override `X402_EIP712_NAME/VERSION`; payload `tokenName`/`tokenVersion` win).
   These MUST match the deployed token's real EIP-2612 domain (e.g. mainnet USDC
   version "2"). A wrong domain makes ecrecover fail closed (good), but an
   operator-supplied wrong default would reject valid permits — verify each
   token's domain before enabling.
2. **On-chain `nonces(owner)` + deadline + deadline-in-past**: the scaffold
   verifies signature *authenticity only*. It does NOT read the token's
   `nonces(owner)` counter (replay), current block deadline validity, or the
   chain's actual chainId at block. These are RPC reads — the sponsoring node must
   `eth_call nonces(owner)` and check `deadline > block.timestamp` before
   broadcasting `settleWithPermit`. **REQUIRED before a real sponsor key is used.**
3. **Replay of permit at settlement**: two payments carrying the same permit
   nonce would both locally "verify" but only the first `permit()` executes
   on-chain; the second reverts. Unless the payment-id idempotency store is
   Redis-backed and consulted, a replay could be double-credited. Wire permit
   nonce + token into the existing tx_hash/payment-id replay store keying.
4. **No fabricated tx hashes**: `prepare_payment` returns `tx_hash: None`
   (except via a real `sponsor_impl`). Any caller wiring a synthetic hash here is
   a bug — keep the contract.
