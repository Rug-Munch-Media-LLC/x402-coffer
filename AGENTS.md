# AGENTS.md — {{PRODUCT_TITLE}}

> AI agent contract. Read this before touching anything in this repo.

## Status
canonical · owner=crmuncher · last_updated={{TODAY}}

## What This Repo Is
{{PRODUCT_PURPOSE}}

## Type
`{{PRODUCT_TYPE}}` · language={{PRODUCT_LANGUAGE}}

## Where It Lives
- Source: `https://git.rugmunch.io/RugMunchMedia/{{PRODUCT_SLUG}}`
- Deployed: {{DEPLOY_TARGET}}
- Port: {{PORT}}

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

## Plan
[PLAN.md](PLAN.md) shows current sprint. Update weekly.

## Status
[STATUS.md](STATUS.md) shows where we are RIGHT NOW. Update before each commit.

## Owners
- Primary: crmuncher
- Backup: TBD

## Related Repos
See [standards/PRODUCTS.md](https://git.rugmunch.io/RugMunchMedia/standards/raw/branch/main/PRODUCTS.md).
