# Security Policy

## Supported Versions

Only the latest release receives security updates. We backport critical CVEs to the current minor series only.

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |
| < latest | ❌       |

## Security Disclaimer

> [!CAUTION]
> **This software has NOT been independently audited.**

Coffer is a payment facilitator — it processes real financial transactions on-chain. Bugs or vulnerabilities **may result in irreversible loss of funds**.

- **No warranty.** Provided "AS IS" without warranty of any kind, express or implied, including but not limited to merchantability, fitness for a particular purpose, and non-infringement.
- **Unaudited.** The codebase has not undergone a formal third-party security audit. Undiscovered vulnerabilities may exist despite extensive testing.
- **Use at your own risk.** The authors and contributors accept no responsibility for financial losses, damages, or other liabilities arising from the use of this software.
- **Testnet first.** Always validate on testnet (Base Sepolia, Solana Devnet) before deploying to mainnet.
- **Key management.** Users are solely responsible for the secure handling of private keys and signing credentials. Never commit keys to git. Use environment variables or a secrets manager (gopass, AWS KMS, HashiCorp Vault).

## Security Model

### Threat Model

Coffer is designed to mitigate the following threats:

| Threat | Mitigation |
| ------ | ---------- |
| **Replay attacks** | Nonce store (in-memory + optional Redis SETNX), payment TTL, caller binding |
| **Underpayment** | Server-side pricing is authoritative; client-supplied amounts are rejected |
| **Man-in-the-middle** | TLS termination at facilitator; all RPC calls over HTTPS |
| **Key compromise** | Hot wallet only; operators should use a dedicated settlement key with limited funds |
| **RPC failure** | Health-aware facilitator router with automatic failover |
| **Double-spend** | Idempotent settlement (already-settled payments return existing tx hash) |
| **Input injection** | Pydantic validation on all inputs; strict JSON schema |
| **Denial of service** | Rate limiting middleware; bounded queues; timeouts on all external calls |

### Fail-Closed Design

Every verification path defaults to **unpaid** on any ambiguity:

- Missing payment → 402
- Expired payment → 402
- Caller mismatch → 402
- RPC timeout → 402
- Malformed response → 402
- Unconfigured wallet → 402 (never claims verified)

### Key Security Properties

1. **Server-side pricing is authoritative.** Client-supplied `amount_usd` is validated against the server-side price table. Underpayment is rejected before any on-chain call.
2. **Replay hardening.** Three layers: payment TTL (default 1h), caller IP binding, EIP-3009 nonce store.
3. **No silent failures.** Every error path returns `verified: false` with a reason. No bare 200 OK as proof of payment.
4. **Scaffolded chains never report verified.** TRON (pre-verifier) was explicitly marked `scaffolded:needs_facilitator`. Now all chains (EVM, Solana, TRON) are live with real on-chain verification.

## Security Best Practices for Operators

### Key Management

```bash
# DO: Load from environment
export X402_FACILITATOR_KEY=$(gopass show x402/settlement-key)
export X402_FACILITATOR_RPC=$(gopass show x402/rpc-url)

# DON'T: Hardcode in config files
# X402_FACILITATOR_KEY=0xabc...  # NEVER do this
```

### Network Hardening

1. **Bind to localhost** when the facilitator is co-located with the app:
   ```bash
   X402_HOST=127.0.0.1 X402_PORT=8080
   ```
2. **Use a reverse proxy** (nginx, Caddy) with TLS termination.
3. **Rate limit** at the reverse proxy level (see `middleware.py`).

### Monitoring

- Log all verification attempts (success and failure)
- Alert on repeated failures from a single IP (potential probe)
- Monitor settlement tx hashes on-chain for confirmation
- Track facilitator health via `/health` endpoint

### Operational Security

1. **Use a dedicated settlement key** — never your main treasury key.
2. **Keep only operational funds** in the settlement wallet (enough for gas + buffer).
3. **Rotate keys** on any suspicion of compromise.
4. **Monitor on-chain** for unexpected outgoing transactions.

## Compliance

### OFAC / Sanctions Screening

Coffer does **not** perform OFAC KYT/OFAC screening on transactions. Operators facilitating payments in regulated jurisdictions must implement their own sanctions screening. We recommend:

- Screening the `from` address of every payment against the OFAC SDN list
- Rejecting payments from sanctioned addresses
- Maintaining an audit trail of all verification/settlement decisions

### KYC / AML

Coffer is a **non-custodial** facilitator — it never holds buyer funds. Settlement moves directly from buyer to seller via on-chain transfers. Operators should assess whether their jurisdiction requires KYC/AML for micropayment facilitation.

### Transaction Monitoring

All payment events are logged (payment_id, tx_hash, network, amount, caller_key, timestamp). Operators should:

- Retain logs for the period required by local regulations (typically 5 years)
- Monitor for suspicious patterns (rapid-fire micro-payments, round amounts, etc.)
- Report suspicious activity to the appropriate authorities

## Reporting a Vulnerability

If you believe you have found a security vulnerability in Coffer, please report it **privately**:

**DO NOT** report vulnerabilities through public GitHub issues, discussions, or pull requests.

1. Open a [Security Advisory](https://git.rugmunch.io/RugMunchMedia/x402-coffer/security/advisories/new)
2. Or email: `admin@rugmunch.io` (if you prefer encrypted communication)

Include as much detail as possible:

- Type of issue (reentrancy, signature malleability, replay, etc.)
- Full path(s) of affected source file(s)
- Commit/branch/tag where the issue exists
- Step-by-step reproduction instructions
- Proof-of-concept code if available
- Potential impact assessment

### Response Timeline

- **Acknowledgment:** Within 3 business days
- **Triage:** Within 7 business days (confirmed / false positive)
- **Fix:** Within 90 days for confirmed vulnerabilities (coordinated disclosure)

We do not currently offer a bug bounty program, but we will work with researchers to ensure findings are properly addressed and credit them in the CHANGELOG.

## Security Audit Status

| Scope | Status | Date | Auditor |
| ----- | ------ | ---- | ------- |
| Core verification logic | ⏳ Pending | — | — |
| Settlement broadcast | ⏳ Pending | — | — |
| Facilitator router | ⏳ Pending | — | — |
| TRON TRC-20 verifier | ⏳ Pending | — | — |

We welcome audit proposals from reputable security firms. Contact `admin@rugmunch.io`.

## Known Limitations

1. **Nonce store is in-memory by default.** For multi-worker deployments, use Redis-backed nonces (`X402_NONCE_STORE=redis`).
2. **No built-in rate limiting on verify/settle.** Operators must add middleware or reverse-proxy rate limiting.
3. **Settlement key is hot.** Operators should minimize funds in the settlement wallet.
4. **No OFAC screening.** Operators must implement their own sanctions compliance.
5. **RPC trust.** The facilitator trusts its configured RPC endpoints. Use reputable providers or self-hosted nodes.

## Security Changelog

See [CHANGELOG.md](CHANGELOG.md) for security-related entries.
