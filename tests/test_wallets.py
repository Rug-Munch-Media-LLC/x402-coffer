"""Tests for wallet management — validation, masking, status."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coffer._engine.wallets import (
    CHAIN_INFO,
    WalletConfig,
    load_wallets,
    validate_wallet,
    mask_wallet,
    get_configured_chains,
    get_unconfigured_chains,
    wallet_status,
)


# ── Address validation ────────────────────────────────────────────────────────


class TestValidateWallet:
    def test_valid_evm(self):
        result = validate_wallet("0x5B38Da6a701c568545dCfcB03FcB875f56beddC4", "base")
        assert result["valid"] is True
        assert result["error"] is None

    def test_invalid_evm_short(self):
        result = validate_wallet("0x1234", "base")
        assert result["valid"] is False
        assert "40 hex" in result["error"]

    def test_invalid_evm_no_prefix(self):
        result = validate_wallet("5B38Da6a701c568545dCfcB03FcB875f56beddC4", "ethereum")
        assert result["valid"] is False

    def test_valid_solana(self):
        result = validate_wallet("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJRMxP2V", "solana")
        assert result["valid"] is True

    def test_invalid_solana_too_short(self):
        result = validate_wallet("short", "solana")
        assert result["valid"] is False

    def test_valid_tron(self):
        result = validate_wallet("TJRyWw8tmq4oG6jYm2aQ9n8p2s5t7u9v1w3x5y7z", "tron")
        assert result["valid"] is True

    def test_invalid_tron_no_t(self):
        result = validate_wallet("1234567890123456789012345678901234567", "tron")
        assert result["valid"] is False

    def test_unknown_chain(self):
        result = validate_wallet("0x1234", "cardano")
        assert result["valid"] is False
        assert "unknown chain" in result["error"]

    def test_empty_address(self):
        result = validate_wallet("", "base")
        assert result["valid"] is False

    def test_sentinel_address(self):
        result = validate_wallet("coffer-unconfigured-wallet", "base")
        assert result["valid"] is False


# ── Masking ───────────────────────────────────────────────────────────────────


class TestMaskWallet:
    def test_mask_evm(self):
        masked = mask_wallet("0x5B38Da6a701c568545dCfcB03FcB875f56beddC4")
        assert masked.startswith("0x5B38")
        assert masked.endswith("ddC4")
        assert "..." in masked

    def test_mask_unconfigured(self):
        assert mask_wallet("") == "(unconfigured)"
        assert mask_wallet("coffer-unconfigured-wallet") == "(unconfigured)"

    def test_mask_short(self):
        masked = mask_wallet("TJRyWw8tmq4oG6jY")
        assert "..." in masked


# ── Config loading ─────────────────────────────────────────────────────────────


class TestWalletConfig:
    def test_load_from_env(self, monkeypatch):
        monkeypatch.setenv("X402_WALLET_BASE", "0x5B38Da6a701c568545dCfcB03FcB875f56beddC4")
        monkeypatch.setenv("X402_WALLET_SOLANA", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJRMxP2V")
        config = load_wallets()
        assert config.is_configured("base")
        assert config.is_configured("solana")
        assert not config.is_configured("ethereum")

    def test_unconfigured_sentinel(self):
        config = WalletConfig(wallet_base="coffer-unconfigured-wallet")
        assert not config.is_configured("base")

    def test_get_unknown_chain(self):
        config = WalletConfig()
        assert config.get("cardano") == ""


# ── Status ─────────────────────────────────────────────────────────────────────


class TestWalletStatus:
    def test_all_unconfigured(self):
        config = WalletConfig()
        status = wallet_status(config)
        assert status["ready"] is False
        assert status["configured_count"] == 0
        assert status["unconfigured_count"] == len(CHAIN_INFO)

    def test_partial_config(self):
        config = WalletConfig(wallet_base="0x5B38Da6a701c568545dCfcB03FcB875f56beddC4")
        status = wallet_status(config)
        assert status["ready"] is True
        assert status["configured_count"] == 1
        assert "base" in status["configured"]
        assert "ethereum" in status["unconfigured"]

    def test_configured_has_masked_address(self):
        config = WalletConfig(wallet_base="0x5B38Da6a701c568545dCfcB03FcB875f56beddC4")
        status = wallet_status(config)
        assert "address_masked" in status["configured"]["base"]
        assert "..." in status["configured"]["base"]["address_masked"]

    def test_unconfigured_has_env_var(self):
        config = WalletConfig()
        status = wallet_status(config)
        assert "env_var" in status["unconfigured"]["base"]
        assert status["unconfigured"]["base"]["env_var"] == "X402_WALLET_BASE"
