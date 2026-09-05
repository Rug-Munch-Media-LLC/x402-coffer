"""Coffer wallet CLI — manage receiving payment wallets.

Usage::

    python -m coffer.wallet_cli show
    python -m coffer.wallet_cli validate --chain base --address 0x...
    python -m coffer.wallet_cli status
    python -m coffer.wallet_cli init
"""

from __future__ import annotations

import os
import sys
from typing import Any

from ._engine.wallets import (
    CHAIN_INFO,
    WalletConfig,
    get_configured_chains,
    get_unconfigured_chains,
    load_wallets,
    mask_wallet,
    validate_wallet,
    wallet_status,
)


def cmd_show() -> None:
    """Show currently configured wallets (masked)."""
    config = load_wallets()
    configured = get_configured_chains(config)
    if not configured:
        print("No wallets configured. Run: python -m coffer.wallet_cli init")
        return
    print(f"\n{'Chain':<12} {'Address (masked)':<20} {'Valid':<6} {'Env Var'}")
    print("-" * 70)
    for chain in configured:
        info = CHAIN_INFO[chain]
        addr = config.get(chain)
        valid = validate_wallet(addr, chain)["valid"]
        print(f"{info.name:<12} {mask_wallet(addr):<20} {'✅' if valid else '❌':<6} {info.env_var}")
    unconfigured = get_unconfigured_chains(config)
    if unconfigured:
        print(f"\nUnconfigured: {', '.join(CHAIN_INFO[c].name for c in unconfigured)}")
    print()


def cmd_validate(chain: str, address: str) -> None:
    """Validate a wallet address for a chain."""
    result = validate_wallet(address, chain)
    if result["valid"]:
        print(f"✅ Valid {CHAIN_INFO[chain].name} address: {result['address']}")
    else:
        print(f"❌ Invalid {chain} address: {result['error']}")
        sys.exit(1)


def cmd_status() -> None:
    """Show which chains are ready to accept payments."""
    status = wallet_status()
    print(f"\nWallet Status: {status['configured_count']}/{status['total_chains']} chains configured")
    if status["ready"]:
        print("✅ Ready to accept payments on configured chains")
    else:
        print("❌ No wallets configured — cannot accept payments")
    if status["configured"]:
        print("\nConfigured:")
        for chain, info in status["configured"].items():
            v = "✅" if info["valid"] else "❌"
            print(f"  {info['name']:<12} {info['address_masked']:<20} {v}")
    if status["unconfigured"]:
        print("\nUnconfigured:")
        for chain, info in status["unconfigured"].items():
            print(f"  {info['name']:<12} set {info['env_var']}")
    print()


def cmd_init() -> None:
    """Interactive wallet setup wizard."""
    print("\n🪙 Coffer Wallet Setup Wizard")
    print("Enter your receiving wallet address for each chain.")
    print("Press Enter to skip a chain.\n")

    env_lines: list[str] = []
    for chain, info in CHAIN_INFO.items():
        current = os.environ.get(info.env_var, "")
        prompt = f"{info.name} ({info.chain_type})"
        if current:
            prompt += f" [{mask_wallet(current)}]"
        prompt += ": "
        addr = input(prompt).strip()
        if addr and addr not in ("", "skip"):
            result = validate_wallet(addr, chain)
            if result["valid"]:
                env_lines.append(f"{info.env_var}={addr}")
                print(f"  ✅ Valid")
            else:
                print(f"  ❌ Invalid: {result['error']}")
                retry = input("  Re-enter or skip [s]: ").strip()
                if retry and retry != "s":
                    result2 = validate_wallet(retry, chain)
                    if result2["valid"]:
                        env_lines.append(f"{info.env_var}={retry}")
                        print("  ✅ Valid")
                    else:
                        print(f"  ❌ Still invalid — skipping")

    if env_lines:
        env_path = ".env"
        existing = ""
        if os.path.exists(env_path):
            with open(env_path) as f:
                existing = f.read()
        with open(env_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# Coffer wallet configuration\n")
            for line in env_lines:
                f.write(line + "\n")
        print(f"\n✅ Wrote {len(env_lines)} wallet(s) to {env_path}")
        print("Run: python -m coffer.wallet_cli status")
    else:
        print("\nNo wallets configured.")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m coffer.wallet_cli <command> [args]")
        print("Commands: show, validate, status, init")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "show":
        cmd_show()
    elif cmd == "validate":
        if len(sys.argv) < 4:
            print("Usage: python -m coffer.wallet_cli validate --chain <chain> --address <addr>")
            sys.exit(1)
        chain = ""
        address = ""
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--chain":
                chain = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--address":
                address = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        cmd_validate(chain, address)
    elif cmd == "status":
        cmd_status()
    elif cmd == "init":
        cmd_init()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: show, validate, status, init")
        sys.exit(1)


if __name__ == "__main__":
    main()
