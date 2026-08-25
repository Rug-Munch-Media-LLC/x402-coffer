# WalletPress — Security Model

> **Status:** Canonical. Owner: WalletPress Security.
> **Last updated:** 2026-07-27.
> **Audience:** Every engineer shipping features. Read before writing any code that touches keys, signatures, payments, or user data.
> **Purpose:** Define the threat model, the trust boundaries, and the rules every PR must follow.

---

## TL;DR — Read this first

**WalletPress holds users' private keys.** That makes it a high-value target. The threat model is: assume the host machine is compromised, the network is hostile, and the AI agent is being prompt-injected. Design so that even under those conditions, the worst case is contained.

The non-negotiables are:

1. **Keys never leave the host unencrypted.** Encrypted at rest with AES-256-GCM + Argon2id. Wrapped with a KEK that lives in a file with mode 0600 or a KMS.
2. **Every cryptographic operation is reproducible.** Anyone with the same mnemonic + path produces the same address. No "secret sauce."
3. **The audit trail is integrity-chained.** Not just append-only — SHA-256 hash chain so tampering is detectable.
4. **Default-deny on all sensitive operations.** Auth required. Role required. Confirmation required. Then allow.

---

## Threat model

### Actors

| Actor | Capability | Goal |
|-------|-----------|------|
| **Honest user** | Owns a wallet, generates addresses | Use the product safely |
| **Comprised operator** | Read access to disk, env vars, network | Steal keys, log secrets |
| **Compromised AI agent** | Prompt injection, malicious tool args | Move funds, leak keys |
| **Network attacker** | MITM, replay, packet injection | Hijack sessions, forge receipts |
| **Insider threat (dev)** | Repo access | Plant backdoor, exfiltrate via build pipeline |
| **External attacker** | Internet-facing endpoint access | Steal credits, brute force, DoS |

### Assets (in priority order)

1. **User private keys** — loss = loss of funds. P0.
2. **User mnemonics** — same as keys. P0.
3. **Vault password (KEK)** — loss = loss of every wallet in the vault. P0.
4. **Receipt signing key** — loss = ability to forge order receipts. P1.
5. **User PII** (email, password, name) — for hosted. P1.
6. **Audit trail** — loss = loss of forensic record. P1.
7. **x402 marketplace credits** — loss = theft of service. P1.

### Trust boundaries

```
Internet → load balancer → FastAPI
                │
Browser/Plugin ─┤
                │
MCP Client ─────┤
                │
                ▼
      WALLETPRESS PROCESS
                │
                ▼
    Middleware stack (in order)
      RequestIDMiddleware, CORSMiddleware,
      RateLimitMiddleware, MetricsMiddleware,
      IPAllowlistMiddleware (admin-only)
                │
                ▼
    Routers → Core (trust zone, internal)
      Vault (AES-GCM keys)
      Auth.KeyStore
      Audit (JSONL append-only)
      AgentSafety (HITL, kill switch)
      ProofOfGeneration (Merkle, signing)
                │
                ▼
        SQLite DB files (mode 0600)
        KEK file (mode 0600)
        Receipt signing key (mode 0600)
```

### Out of scope (assume compromised)

- The host machine OS kernel
- The Docker runtime
- The cloud provider
- The user's browser (relies on TLS + WP plugin auth)

---

## Key management

### At rest

| Asset | Storage | Encryption | Access |
|-------|---------|-----------|--------|
| User wallet private key | `wallets.encrypted_key` (SQLite) | AES-256-GCM, key from KEK + per-row salt | Vault code path only |
| KEK (vault password) | Environment variable or file | n/a — this IS the key | OS file perms |
| Receipt signing key | `data_dir / .receipt_signing_key` | mode 0600, plaintext | core/proof.py only |
| Hosted user password | `users.password_hash` (SQLite) | Argon2id | login flow only |
| API keys | `keys_path` JSON file | SHA-256 with random salt | Auth.KeyStore only |
| Mnemonic | Returned to caller once, then zeroed in memory | n/a | Generator + caller only |

### In transit

- TLS required in production (enforce at reverse proxy).

### In memory

- Private keys held as `bytearray` in `GeneratedWallet._private_key_bytes`. Cleared on `clear_sensitive()`.
- **Gap:** `private_key_hex` (str) and `mnemonic` (str) are Python strings — cannot be zeroed. Use `bytes` everywhere internally.

---

## Authentication & authorization

### Layers

1. **TLS**: certbot + Caddy or nginx.
2. **IP allowlist**: `WP_ALLOWED_IPS` env var, comma-separated CIDRs.
3. **API key**: `X-API-Key` header or `Authorization: Bearer`. Validated by `auth.KeyStore.verify()`.
4. **Admin key**: `WP_ADMIN_KEY` env var. Bypasses key store. For bootstrap only.
5. **Scoped permissions**: `APIKey.scopes` like `["wallet.read", "wallet.write", "admin"]`. Enforced via `require_scope()` dependency.

---

## Cryptography

### Algorithms

| Use | Algorithm | Status |
|-----|-----------|--------|
| Symmetric encryption | AES-256-GCM | OK |
| KDF | Argon2id | OK |
| Receipt signing | Ed25519 (NaCl) | OK |
| User passwords (hosted) | Argon2id | OK |
| API key hashing | SHA-256 with salt | OK |
| Wallet derivation | BIP39 → BIP32 (Ed25519 SLIP-10, secp256k1) | OK |

### Source of randomness

`secrets` module is used throughout. OK.

---

## Network security

### Inbound

- Public ingress only via reverse proxy with TLS.
- IP allowlist middleware for admin endpoints.

### Outbound

- **Public RPC endpoints** configurable in chains.py. Override via `WP_RPC_{CHAIN}` env vars.
- **AI provider API calls** go to user-configured endpoints. No telemetry.
- **Webhook deliveries** POST to user-configured URLs. Webhook signing (HMAC-SHA-256) is present.

### Rate limiting

- **Per-IP** token bucket (`rate_limit.RateLimitMiddleware`). Configurable default.
- **Per-API-key** bucket — partially supported.

---

## Operational security

### Logging

- `log_requests` middleware logs method, path, status, elapsed, request_id.
- **Gap:** Ensure no auth headers or private keys appear in error messages.

### Error handling

- Global exception handler returns clean JSON `{error, request_id}`.
- HTTPException handled with status code preserved.

### Monitoring

- `MetricsMiddleware` for Prometheus-format metrics.
- `/health` returns 200/503 with subsystem status.

### Backups

- `walletpress backup` CLI exports data. Should encrypt the archive (AES-GCM with passphrase).
- **Target:** Off-site replication of vault DB + key material.

---

## Security checklist (pre-release)

Run before every tagged release:

```bash
# 1. Lint + type + tests
make check

# 2. Dependency audit
cd backend && safety check && pip-audit

# 3. Static analysis
cd backend && bandit -r . && semgrep --config=auto .

# 4. Secrets scan
gitleaks detect --redact

# 5. Verify no broken chains are enabled
python3 -c "
from wallet_engine.chains import CHAINS
BROKEN = {'atom', 'osmo', 'inj', 'juno', 'sei', 'evmos', 'algo', 'xtz', 'xlm', 'ton', 'xno', 'fil', 'dot', 'ksm', 'xrp', 'ada', 'bch', 'xmr', 'zec'}
live = set(CHAINS.keys()) & BROKEN
assert not live, f'Broken chains still enabled: {live}'
print('OK')
"
```

---

## What NOT to do

These have come up in code review and are explicitly forbidden:

1. **Do NOT log private keys or mnemonics.** Redact before logging.
2. **Do NOT hardcode secrets in code or .env files committed to git.**
3. **Do NOT skip auth on "internal" endpoints.** Anything that mutates state needs auth.
4. **Do NOT trust LLM output as input.** Validate JSON schema. Reject unknown tool names.
5. **Do NOT trust user-supplied chains.yaml.** Validate every field.
6. **Do NOT return private keys in the response body unless the user explicitly opted in via a flag.**
7. **Do NOT use `==` to compare secrets.** Use `hmac.compare_digest`.
8. **Do NOT use `requests`.** Use `httpx.AsyncClient`.
9. **Do NOT use `time.sleep`.** Use `asyncio.sleep`.
10. **Do NOT use bare `except:` or `except Exception:` without re-raising or logging.
