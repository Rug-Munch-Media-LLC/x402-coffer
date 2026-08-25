# Contributing to WalletPress

WalletPress is open source (MIT). We welcome contributions of all kinds:
bug fixes, new chain support, documentation, tests, and features.

## Getting Started

```bash
git clone https://github.com/cryptorugmuncher/walletpress
cd walletpress/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export WP_ADMIN_KEY=dev-key
export WP_VAULT_PASSWORD=dev-pass
uvicorn main:app --port 8010 --reload
```

## Code Standards

- Python 3.10+ with type hints
- Follow existing patterns (FastAPI, Pydantic v2, asyncio)
- One PR per feature. Small commits with conventional messages
- Add tests for new chain support (see `scripts/verify_test_vectors.py`)
- No hardcoded secrets, no telemetry, no third-party data sharing

## Adding a Chain

1. Add chain config in `wallet_engine/chains.py`
2. Add generation method in `wallet_engine/generator.py`
3. Add balance fetching in `routers/balance_fetcher.py`
4. Add test vector in `scripts/verify_test_vectors.py`
5. Add to `supported_chains()` in the WP plugin

## Verification

All contributions must pass:
```bash
./venv/bin/python scripts/verify_test_vectors.py
# Output: "ALL BIP39 VECTORS VERIFIED"
```

## PR Process

1. Fork the repo
2. Create a branch: `feat/add-zcash-support`
3. Commit with conventional message: `feat: add Zcash (ZEC) wallet generation`
4. Push and create PR
5. Ensure CI passes (tests + format + type check)

## Security

Report vulnerabilities to security@walletpress.cc, not via public issues.
We respond within 48 hours.

## License

By contributing, you agree your work is licensed under the MIT License.
