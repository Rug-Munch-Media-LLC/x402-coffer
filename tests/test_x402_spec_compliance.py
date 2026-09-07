"""Spec-compliance + pry billing-parity tests (2026-09-06 x402-coffer audit).

Covers the post-audit hardening, item by item:

1. ``POST /v1/x402/settle`` — idempotent + replay-checked: requires a prior
   successful verify, stores the settle outcome, and a repeat settle returns
   the stored result instead of re-rolling the facilitator.
2. RFC 7807 error contract (``application/problem+json``) + body-size caps +
   schema validation on the verify/settle wire paths.
3. ``GET /health`` liveness (payment store + Redis mirror status).
4. ``PaymentStore`` payment_id format/length validation.
5. RateLimiter / SpendingPolicy wired into the verify/settle orchestration.
6. Config: facilitator signing key from env/file secret, fail-fast in prod.
8. Pry billing parity: invoice reconcile (recipient+amount+asset) gates settle.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    ASSET,
    CAIP2_BASE,
    REQUIRED_ATOMIC,
    SERVICE_PRICE_USD,
    X402E2EHarness,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coffer._engine.middleware import get_default_store
from coffer._engine.router import MAX_REQUEST_BODY_BYTES, health_router
from coffer._engine.store import PaymentStore, validate_payment_id

SENDER_IP = "1.1.1.1"
OTHER_IP = "2.2.2.2"


def _pid(tag: str) -> str:
    return f"pay_{tag}_{'0' * 16}"


async def _pay(
    e2e: X402E2EHarness,
    client: httpx.AsyncClient,
    *,
    tx_hash: str,
    payment_id: str,
    paid_atomic: int = REQUIRED_ATOMIC,
    amount_usd: float = SERVICE_PRICE_USD,
    network: str = CAIP2_BASE,
    asset: str = ASSET,
) -> httpx.Response:
    """Record an on-chain payment then POST the real verify endpoint."""
    e2e.facilitator.record_payment(tx_hash, amount_atomic=paid_atomic)
    return await client.post(
        "/v1/x402/verify",
        json={
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
        },
    )


async def _settle(
    client: httpx.AsyncClient,
    *,
    payment_id: str,
    tx_hash: str,
    amount_usd: float = SERVICE_PRICE_USD,
    network: str = CAIP2_BASE,
    asset: str = ASSET,
) -> httpx.Response:
    return await client.post(
        "/v1/x402/settle",
        json={
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
        },
    )


# ── 1. idempotent + replay-checked settle ───────────────────────────────────


class TestSettleIdempotent:
    async def test_settle_without_prior_verify_is_409(self, e2e: X402E2EHarness) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await _settle(client, payment_id=_pid("noverify"), tx_hash="0xtx_nv")
        assert r.status_code == 409
        body = r.json()
        assert body["status"] == 409
        assert "verify" in str(body["detail"]).lower()
        assert "error" in body  # legacy alias kept
        assert r.headers["content-type"].startswith("application/problem+json")

    async def test_settle_after_verify_succeeds_and_repeats_are_stored(self, e2e: X402E2EHarness) -> None:
        tx = "0xtx_idem"
        pid = _pid("idem")
        async with e2e.caller(SENDER_IP) as client:
            v = await _pay(e2e, client, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200

            first = await _settle(client, payment_id=pid, tx_hash=tx)
            assert first.status_code == 200
            assert first.json()["success"] is True
            assert e2e.facilitator.settled_ids == [tx]

            # Repeat settle returns the STORED outcome — facilitator untouched.
            second = await _settle(client, payment_id=pid, tx_hash=tx)
            assert second.status_code == 200
            assert e2e.facilitator.settled_ids == [tx]
            first_body = first.json()
            second_body = second.json()
            assert second_body.pop("idempotent_replay") is True
            assert second_body == first_body

    async def test_settle_from_different_caller_is_409(self, e2e: X402E2EHarness) -> None:
        tx = "0xtx_caller"
        pid = _pid("caller")
        async with e2e.caller(SENDER_IP) as alice:
            v = await _pay(e2e, alice, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200
        async with e2e.caller(OTHER_IP) as bob:
            r = await _settle(bob, payment_id=pid, tx_hash=tx)
        assert r.status_code == 409
        assert "recipient_mismatch" in r.json()["detail"]
        assert e2e.facilitator.settled_ids == []

    async def test_settle_amount_mismatch_is_409(self, e2e: X402E2EHarness) -> None:
        tx = "0xtx_amt"
        pid = _pid("amt")
        async with e2e.caller(SENDER_IP) as client:
            v = await _pay(e2e, client, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200
            r = await _settle(client, payment_id=pid, tx_hash=tx, amount_usd=0.02)
        assert r.status_code == 409
        assert "amount_mismatch" in r.json()["detail"]
        assert e2e.facilitator.settled_ids == []

    async def test_settle_asset_mismatch_is_409(self, e2e: X402E2EHarness) -> None:
        tx = "0xtx_asset"
        pid = _pid("asset")
        async with e2e.caller(SENDER_IP) as client:
            v = await _pay(e2e, client, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200
            r = await _settle(client, payment_id=pid, tx_hash=tx, asset="USDT")
        assert r.status_code == 409
        assert "asset_mismatch" in r.json()["detail"]
        assert e2e.facilitator.settled_ids == []

    async def test_settle_expired_verify_is_409(self, e2e: X402E2EHarness) -> None:
        tx = "0xtx_exp"
        pid = _pid("exp")
        async with e2e.caller(SENDER_IP) as client:
            v = await _pay(e2e, client, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200
        store = get_default_store()
        store._records[pid]["expires_at"] = "2000-01-01T00:00:00+00:00"
        async with e2e.caller(SENDER_IP) as client:
            r = await _settle(client, payment_id=pid, tx_hash=tx)
        assert r.status_code == 409
        assert e2e.facilitator.settled_ids == []


# ── 2. RFC 7807 error contract + body caps + schema validation ──────────────


class TestRfc7807:
    async def test_missing_fields_is_problem_json(self, e2e: X402E2EHarness) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/verify", json={})
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        for member in ("type", "title", "status", "detail"):
            assert member in body
        assert body["status"] == 400
        assert body["error"] == body["detail"]  # legacy alias preserved
        assert "payment_id" in str(body["detail"])

    async def test_amount_rejection_is_problem_json(self, e2e: X402E2EHarness) -> None:
        payload = {
            "payment_id": _pid("amt0"),
            "tx_hash": "0xtx_amt0",
            "network": CAIP2_BASE,
            "asset": ASSET,
            "amount_usd": 0,
        }
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/verify", json=payload)
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")
        assert "amount" in r.json()["error"].lower()

    async def test_oversized_body_is_413(self, e2e: X402E2EHarness) -> None:
        blob = "x" * (MAX_REQUEST_BODY_BYTES + 1)
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post(
                "/v1/x402/verify",
                content=blob,
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 413
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["status"] == 413

    async def test_non_object_json_is_400_not_500(self, e2e: X402E2EHarness) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post(
                "/v1/x402/verify",
                content="[1, 2, 3]",
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 400
        assert "object" in r.json()["detail"].lower()

    async def test_invalid_json_is_400_problem(self, e2e: X402E2EHarness) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post(
                "/v1/x402/verify",
                content="{not json",
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")

    async def test_schema_rejects_non_string_fields(self, e2e: X402E2EHarness) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post(
                "/v1/x402/verify",
                json={"payment_id": ["x"], "tx_hash": "0x1", "amount_usd": 0.01},
            )
        assert r.status_code == 400
        assert "must be a string" in r.json()["detail"]

    async def test_underpayment_still_legacy_402_contract(self, e2e: X402E2EHarness) -> None:
        """The 402/410 acceptance surface is untouched by the RFC 7807 work."""
        async with e2e.caller(SENDER_IP) as client:
            r = await _pay(
                e2e,
                client,
                tx_hash="0xtx_under2",
                payment_id=_pid("under2"),
                paid_atomic=REQUIRED_ATOMIC - 1,
            )
        assert r.status_code == 402
        assert r.json().get("x402Version") == 1
        assert "underpayment" in r.json()["error"].lower()


# ── 3. /health liveness ──────────────────────────────────────────────────────


class TestLiveness:
    def test_health_exposes_store_and_redis_status(self) -> None:
        app = FastAPI()
        app.include_router(health_router)
        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["store"]["store"] == "ok"
        assert "redis" in body["store"]
        assert isinstance(body["store"]["payments"], int)


# ── 4. payment_id format/length validation ──────────────────────────────────


class TestPaymentIdValidation:
    def test_validate_payment_id_accepts_canonical(self) -> None:
        for good in ("p0", "pay_abc_1234567890ABCDEF", "A" * 128, "pay-roundtrip_0000000000000000"):
            assert validate_payment_id(good) == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            " ",
            "has space",
            "has/slash",
            "uni\N{SNOWMAN}",
            'quote"inject',
            "x" * 129,
        ],
    )
    def test_validate_payment_id_rejects_malformed_or_oversized(self, bad: str) -> None:
        assert validate_payment_id(bad) != ""

    def test_store_record_rejects_invalid_payment_id(self) -> None:
        store = PaymentStore()
        with pytest.raises(ValueError, match="payment_id"):
            store.record(payment_id="bad id", caller_key="c1", amount_usd=0.01)
        assert "bad id" not in store._records

    def test_store_record_accepts_short_legacy_ids(self) -> None:
        store = PaymentStore()
        store.record(payment_id="p0", caller_key="c1", amount_usd=0.01, verified=True)
        assert store.is_paid("p0", "c1") is True


# ── 5. RateLimiter / SpendingPolicy wired into verify/settle ────────────────


class TestPolicyWiring:
    async def test_settle_rate_limited_after_verify(
        self, e2e: X402E2EHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import coffer._engine.ratelimit as rl_mod

        monkeypatch.setattr(rl_mod, "_limiter", None)
        monkeypatch.setenv("X402_RATE_LIMIT_PER_MIN", "1")
        tx = "0xtx_rl"
        pid = _pid("rl")
        async with e2e.caller(SENDER_IP) as client:
            v = await _pay(e2e, client, tx_hash=tx, payment_id=pid)
            assert v.status_code == 200  # verify consumes the 1 req/min token
            # The settle is the second request in the window -> rate-limited.
            r = await _settle(client, payment_id=pid, tx_hash=tx)
        assert r.status_code == 429
        assert r.headers["content-type"].startswith("application/problem+json")
        assert "Retry-After" in r.headers
        assert "rate limit" in r.json()["error"].lower()
        assert e2e.facilitator.settled_ids == []

    async def test_verify_over_spend_cap_is_429(
        self, e2e: X402E2EHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import coffer._engine.spending as sp

        monkeypatch.setattr(sp, "_default_policy", None)
        # Cap below the server-required atomic amount for 0.01 USD.
        monkeypatch.setenv("X402_MAX_PER_PAYMENT_ATOMIC", str(REQUIRED_ATOMIC - 1))
        async with e2e.caller(SENDER_IP) as client:
            r = await _pay(
                e2e,
                client,
                tx_hash="0xtx_spend",
                payment_id=_pid("spend"),
            )
        assert r.status_code == 429
        body = r.json()
        assert body["status"] == 429
        assert "over_per_payment_limit" in str(body["detail"])


# ── 6. facilitator signing key: env/file secret + fail-fast ─────────────────


class TestConfigSigningKey:
    def test_key_loaded_from_secret_file(self, tmp_path) -> None:
        from coffer._engine.config import X402Settings

        secret = tmp_path / "facilitator.key"
        secret.write_text("0xabcdef1234\n")
        cfg = X402Settings.from_env({"X402_WALLET": "0xabc", "X402_SELF_FACILITATOR_KEY_FILE": str(secret)})
        assert cfg.self_facilitator_key == "0xabcdef1234"
        assert cfg.self_facilitator_key_file == str(secret)

    def test_env_key_wins_over_file(self, tmp_path) -> None:
        from coffer._engine.config import X402Settings

        secret = tmp_path / "facilitator.key"
        secret.write_text("from-file\n")
        cfg = X402Settings.from_env(
            {
                "X402_WALLET": "0xabc",
                "X402_SELF_FACILITATOR_KEY": "from-env",
                "X402_SELF_FACILITATOR_KEY_FILE": str(secret),
            }
        )
        assert cfg.self_facilitator_key == "from-env"

    def test_missing_file_is_empty_not_fatal_at_parse(self, tmp_path) -> None:
        from coffer._engine.config import X402Settings

        cfg = X402Settings.from_env(
            {"X402_WALLET": "0xabc", "X402_SELF_FACILITATOR_KEY_FILE": str(tmp_path / "absent")}
        )
        assert cfg.self_facilitator_key == ""

    def test_prod_self_hosted_rpc_without_key_fails_fast(self) -> None:
        from coffer._engine.config import X402Settings

        cfg = X402Settings.from_env(
            {
                "X402_WALLET": "0xabc",
                "X402_SELF_FACILITATOR_RPC": "https://rpc.example",
            }
        )
        assert cfg.debug is False
        with pytest.raises(RuntimeError, match="signing key"):
            cfg.assert_configured()

    def test_debug_or_no_rpc_does_not_require_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coffer._engine.config import X402Settings

        # Bool fields are read from the process env (module _env_bool), so the
        # debug flag must be set there for the explicit-mapping construction.
        monkeypatch.setenv("X402_DEBUG", "1")
        debug_cfg = X402Settings.from_env(
            {"X402_WALLET": "0xabc", "X402_SELF_FACILITATOR_RPC": "https://rpc"}
        )
        assert debug_cfg.debug is True
        debug_cfg.assert_configured()  # debug mode: no fail-fast

        monkeypatch.delenv("X402_DEBUG", raising=False)
        no_rpc_cfg = X402Settings.from_env({"X402_WALLET": "0xabc"})
        no_rpc_cfg.assert_configured()  # nothing to sign against

    def test_runtime_module_resolves_file_key(self, tmp_path, monkeypatch) -> None:
        secret = tmp_path / "signer.key"
        secret.write_text("0xdeadbeef\n")
        monkeypatch.delenv("X402_SELF_FACILITATOR_KEY", raising=False)
        monkeypatch.setenv("X402_SELF_FACILITATOR_KEY_FILE", str(secret))
        import importlib

        import coffer._engine.facilitator_config as fc

        importlib.reload(fc)
        assert fc.X402_SELF_FACILITATOR_KEY == "0xdeadbeef"
