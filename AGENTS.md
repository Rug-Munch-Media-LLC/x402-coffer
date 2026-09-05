# AGENTS.md — Coffer

> AI agent contract. Read this before touching anything in this repo.

## Status
canonical · owner=crmuncher · last_updated=2026-09-05

## What This Repo Is
Self-hosted x402 payment facilitator — accept HTTP 402 micropayments in one
line of Python, on your own infrastructure. Your HTTP treasury, sovereign
and open. EVM (EIP-3009/EIP-7702) + Solana (SPL transferChecked) + TRON
(scaffolded). MCP-native for AI agent payments.

## Type
`backend` · language=Python 3.11+ / FastAPI

## Where It Lives
- Source: `https://git.rugmunch.io/RugMunchMedia/x402-coffer`
- Deployed: Docker container (Dockerfile pending — P0 fix)
- Port: 8001

## Built With
- Standards: [git.rugmunch.io/RugMunchMedia/standards](https://git.rugmunch.io/RugMunchMedia/standards)
- Toolchain: [TOOLCHAIN.md](https://git.rugmunch.io/RugMunchMedia/standards/raw/branch/main/TOOLCHAIN.md)
- Conventions: [CONVENTIONS.md](https://git.rugmunch.io/RugMunchMedia/standards/raw/branch/main/CONVENTIONS.md)
- Git topology: [GIT-ARCHITECTURE.md](https://git.rugmunch.io/RugMunchMedia/standards/raw/branch/main/GIT-ARCHITECTURE.md)

## Components
{{COMPONENTS}}

## Dependencies
{{DEPENDENCIES}}

## 🚨 CRITICAL RULES

1. **No nested repos.** Don't commit other complete project trees inside this one.
2. **No secrets.** Never commit `.secrets/`, `.env`, `*.pem`, `*.key`, `*.crt`. Use gopass.
3. **No data blobs.** Don't commit `*.zip`, `*.parquet`, model weights, sqlite files.
4. **No broken files.** Shell heredoc accidents must be caught before commit.
5. **No duplicate scaffolds.** If a `backend/` or `frontend/` subdir exists at root, it's either THE app or bloat.
6. **Update STATUS.md before committing.** Builders must know where we are.
7. **Read DECISIONS.md before architectural changes.** Don't repeat rejected designs.
8. **Use `make <target>`.** Don't run raw `ruff` / `pytest` / `mypy` when a target exists.
9. **Conventional commits.** `feat(scope):`, `fix(scope):`, etc. Use `make commit`.
10. **Never `--no-verify` on pre-commit.** If a hook fails, fix it.

## Commands
```bash
make install     # install deps
make dev         # dev server
make test        # tests
make lint        # ruff + format check
make typecheck   # mypy strict
make security    # bandit + gitleaks
make ci          # full local pipeline
make commit      # conventional commit
make clean       # remove build artifacts
```

## Architecture
See [ARCHITECTURE.md](ARCHITECTURE.md). Update it when components change.

## Security Hardening
Applied 26 hardening fixes: fail-closed pricing, operation gating, settlement idempotency, fsync, thread lock, facilitator TTL, pending_auths cleanup, network validation, TRON replay, webhook HMAC, SSRF guard, redirect disable, empty-secret refuse, dead-letter redaction, TRON contract filter, atomic compare, caller-binding, batch caller_key, rate limit degraded cap, LRU, lazy loading, EVM RPC fallback, Solana payer binding, health-score routing, store fsync, batch expiry, import fix.

## Quality Gates
- 200 tests
- ruff clean
- pyright clean

## Parity with Pry
Coffer was born from pryscraper's payment system, now at full parity.
