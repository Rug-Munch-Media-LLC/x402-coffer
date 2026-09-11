"""Tests for Sign-In-With-X (CAIP-122) extension."""

from __future__ import annotations

import base64
import json

from coffer._engine.extensions.sign_in_with_x import (
    SIWXChallengeStore,
    SIWXSessionStore,
    build_siwx_message,
    verify_siwx_proof,
)

# Test key — deterministic, funded-on-nothing, safe to commit (not a secret).
TEST_KEY = "0x" + "11" * 32
DOMAIN = "d.test"


def _test_addr() -> str:
    from eth_account import Account

    return Account.from_key(TEST_KEY).address.lower()


def _sign(message: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    signed = Account.from_key(TEST_KEY).sign_message(encode_defunct(text=message))
    sig = getattr(signed, "signature", signed)
    if isinstance(sig, bytes):
        return "0x" + sig.hex()
    return str(sig)


def _proof(message: str, signature: str) -> str:
    return base64.b64encode(json.dumps({"message": message, "signature": signature}).encode()).decode()


def test_build_siwx_message_contains_caip122_fields() -> None:
    msg = build_siwx_message(domain=DOMAIN, address="0xabc", chain_id="eip155:8453", nonce="n1")
    assert f"{DOMAIN} wants you to sign in" in msg
    assert "0xabc" in msg
    assert "Chain ID: eip155:8453" in msg
    assert "Nonce: n1" in msg


def test_full_challenge_verify_flow_grants_session() -> None:
    challenges = SIWXChallengeStore()
    created = challenges.create(domain=DOMAIN, chain_id="eip155:8453")
    msg = build_siwx_message(
        domain=DOMAIN, address=_test_addr(), chain_id="eip155:8453", nonce=created["nonce"]
    )
    result = verify_siwx_proof(_proof(msg, _sign(msg)), challenges=challenges, domain=DOMAIN)
    assert result["verified"] is True, result
    assert result["address"] == _test_addr()
    assert result["session"] and result["session"]["session_token"]


def test_replay_rejected_nonce_single_use() -> None:
    challenges = SIWXChallengeStore()
    created = challenges.create(domain=DOMAIN, chain_id="eip155:8453")
    msg = build_siwx_message(
        domain=DOMAIN, address=_test_addr(), chain_id="eip155:8453", nonce=created["nonce"]
    )
    proof = _proof(msg, _sign(msg))
    first = verify_siwx_proof(proof, challenges=challenges, domain=DOMAIN)
    second = verify_siwx_proof(proof, challenges=challenges, domain=DOMAIN)
    assert first["verified"] is True
    assert second["verified"] is False and "challenge" in second["reason"]


def test_domain_mismatch_rejected() -> None:
    challenges = SIWXChallengeStore()
    created = challenges.create(domain="good.test", chain_id="eip155:8453")
    msg = build_siwx_message(
        domain="evil.test", address=_test_addr(), chain_id="eip155:8453", nonce=created["nonce"]
    )
    result = verify_siwx_proof(_proof(msg, _sign(msg)), challenges=challenges, domain="good.test")
    assert result["verified"] is False and "domain" in result["reason"]


def test_forged_signature_rejected() -> None:
    challenges = SIWXChallengeStore()
    created = challenges.create(domain=DOMAIN, chain_id="eip155:8453")
    msg = build_siwx_message(
        domain=DOMAIN, address=_test_addr(), chain_id="eip155:8453", nonce=created["nonce"]
    )
    bad_sig = "0x" + "cd" * 65
    result = verify_siwx_proof(_proof(msg, bad_sig), challenges=challenges, domain=DOMAIN)
    assert result["verified"] is False


def test_malformed_proof_fail_closed() -> None:
    challenges = SIWXChallengeStore()
    for bad in ("", "!!!not-base64!!!", base64.b64encode(b"{}").decode()):
        result = verify_siwx_proof(bad, challenges=challenges, domain=DOMAIN)
        assert result["verified"] is False


def test_signer_must_match_named_account() -> None:
    challenges = SIWXChallengeStore()
    created = challenges.create(domain=DOMAIN, chain_id="eip155:8453")
    other_addr = "0x9999999999999999999999999999999999999999"
    msg = build_siwx_message(
        domain=DOMAIN, address=other_addr, chain_id="eip155:8453", nonce=created["nonce"]
    )
    result = verify_siwx_proof(_proof(msg, _sign(msg)), challenges=challenges, domain=DOMAIN)
    assert result["verified"] is False and "signer" in result["reason"]


def test_session_expiry_enforced() -> None:
    sessions = SIWXSessionStore(ttl_seconds=-1)
    granted = sessions.grant(_test_addr(), "eip155:8453")
    assert sessions.validate(granted["session_token"]) is None
