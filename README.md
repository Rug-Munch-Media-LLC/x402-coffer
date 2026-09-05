<p align="center">
  <img src="https://img.shields.io/badge/coffer-x402%20facilitator-2ea44f?style=for-the-badge" alt="Coffer" />
</p>

<h1 align="center">Coffer — The Sovereign x402 Payment Facilitator</h1>

<p align="center">
  <strong>Pay-per-request HTTP for the multi-chain economy.</strong><br/>
  Coffer is a self-hosted, open-source payment gateway designed for AI agents and microservices. It enables seamless, pay-per-request access to APIs and tools across EVM, Solana, and TRON. By removing reliance on centralized custodians, Coffer provides true sovereignty for the agentic economy.
</p>

<p align="center">
  <a href="https://git.rugmunch.io/RugMunchMedia/x402-coffer/actions"><img src="https://img.shields.io/badge/build-passing-brightgreen" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-000000" /></a>
  <a href="https://www.x402.org/"><img src="https://img.shields.io/badge/x402-v2%20spec-2ea44f" /></a>
</p>

---

## Why Coffer

Unlike centralized payment providers, Coffer is built for developers who demand control, privacy, and multi-chain interoperability.

*   **Sovereign & Self-Hosted:** You own the infrastructure. No third-party visibility into your transaction volume or customer data.
*   **Zero Custodial Risk:** Non-custodial by design. Funds move directly from buyer to seller via on-chain transfers.
*   **Multi-Chain Native:** Support for 6 major networks (EVM×5, Solana, TRON) and 25+ assets out of the box.
*   **MCP Native:** Includes a built-in Model Context Protocol (MCP) server, allowing AI agents to discover and pay for tools automatically.
*   **No Coinbase Dependency:** While it can route to external facilitators, Coffer is fully functional as a standalone, self-hosted settlement engine.
*   **Developer First:** One-line Python decorators and a robust REST/MCP dual interface.

---

## Quick Start

### Installation

```bash
pip install x402-coffer
```

### Basic Usage

Set your receiving wallet via environment variables:

```bash
export X402_WALLET="0xYourEVMAddress"
# For Solana: export X402_WALLET_SOLANA="YourSolanaAddress"
# For TRON: export X402_WALLET_TRON="YourTronAddress"
```

Implement a paywall in your FastAPI application:

```python
from coffer import require_payment

@app.get("/premium-tool")
@require_payment(asset="USDC", network="base", amount=0.001, operation="tool_use")
async def premium_endpoint(request):
    return {"status": "success", "data": "Access granted!"}
```

---

## Architecture

Coffer's settlement engine is modular by design. Large modules are split into focused, single-responsibility files behind a thin facade, so the public API stays stable while internals stay readable:

*   **`coffer/_engine/solana_verifier.py`** — thin facade over four focused modules:
    *   `solana_constants.py` — chain constants, limits, and configuration
    *   `solana_parse.py` — transaction/instruction parsing
    *   `solana_result.py` — verification result types
    *   `solana_transfer.py` — SPL transfer verification logic
*   **`coffer/_engine/models.py`** — thin facade over three focused modules:
    *   `models_payment.py` — payment request/authorization models
    *   `models_settlement.py` — settlement and receipt models
    *   `models_response.py` — verify/settle response models

All modules are kept under 400 lines using the thin facade pattern throughout.

---

## Parity with Pry

Coffer's built-in facilitator matches pry's self-facilitator in verification capabilities:

*   **EVM:** EIP-3009 (transferWithAuthorization) and EIP-7702 verification
*   **Solana:** SPL `transferChecked` on-chain verification with payer binding
*   **TRON:** TRC-20 verification scaffolded

---

## How It Works

Coffer implements the x402 specification to bridge HTTP requests with on-chain settlement.

```text
Client (AI Agent / Browser)        Your Server (Coffer)
  │                                    │
  │  1. GET /resource                  │
  ├───────────────────────────────────►│
  │                                    │
  │  2. HTTP 402 Payment Required      │
  │     PAYMENT-REQUIRED: <payload>    │   ← price, network, asset, payTo
  │◄───────────────────────────────────┤
  │                                    │
  │  3. (Client signs & sends payment)│
  │                                    │
  │  4. GET /resource                  │
  │     PAYMENT-SIGNATURE: <payload>  │
  ├───────────────────────────────────►│
  │                                    │
  │  5. [ Internal Verification ]      │
  │     - Check on-chain transaction  │
  │     - Validate signature/nonce    │
  │                                    │
  │  6. HTTP 200 OK                    │
  │     PAYMENT-RESPONSE: <receipt>   │   ← settlement confirmation
  │◄───────────────────────────────────┤
```

1.  **Request:** The client attempts to access a protected resource.
2.  **Challenge:** Coffer intercepts the request and returns an `HTTP 402` with the required payment details.
3.  **Settlement:** The client executes the transaction on-chain. Coffer verifies the transaction and grants access.

---

## Security

Coffer is engineered for high-stakes production environments with a "fail-closed" philosophy.

*   **Fail-Closed Pricing:** All client-provided amounts are validated against authoritative server-side oracles.
*   **Operation Gating:** Payments are cryptographically bound to specific operations to prevent reuse for different services.
*   **Settlement Idempotency:** Prevents double-spending and duplicate settlement via strict nonce and transaction hash tracking.
*   **FSYNC Durability:** Payment records are persisted with high durability to prevent state loss during crashes.
*   **Thread-Safe Execution:** Internal state management uses robust locking to prevent race conditions during high-concurrency verification.
*   **SSRF Guard:** Strict validation of all callback and webhook URLs to prevent Server-Side Request Forgery.
*   **Replay Protection:** Combines TTL (Time-To-Live) and caller binding to ensure a signature cannot be reused.
*   **HMAC Webhooks:** Secure, signed notifications for all payment events.

---

## Networks

| Network | CAIP-2 | Supported Assets | Verification Method |
| :--- | :--- | :--- | :--- |
| **Base** | `eip155:8453` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Ethereum** | `eip155:1` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Arbitrum** | `eip155:42161` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Optimism** | `eip155:10` | USDC, USDT, ETH | EIP-7702 / EIP-3009 |
| **Polygon** | `eip155:137` | USDC, USDT, POL | EIP-7702 / EIP-3009 |
| **Solana** | `solana:mainnet` | USDC, USDT, SOL | SPL RPC Verification |
| **TRON** | `tron:mainnet` | USDT, TRX | TRC-20 Grid Verification |

---

## Configuration

Configuration is managed via environment variables with the `X402_` prefix.

| Variable | Description |
| :--- | :--- |
| `X402_WALLET` | Primary receiving EVM address. |
| `X402_NETWORK` | Default network (e.g., `base`, `ethereum`, `solana`). |
| `X402_FACILITATOR_URL` | URL for external facilitator routing (optional). |
| `REDIS_URL` | Connection string for the payment nonce/replay store. |
| `X402_OFFLINE_MODE` | Enable for testing without real on-chain verification. |

---

## Comparison

| Feature | Coffer | Coinbase CDP | PayAI | Fireblocks | Mogami |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Open Source (MIT)** | ✅ | ❌ | ❌ | ✅ | ✅ |
| **Self-Hosted** | ✅ | ❌ | ❌ | ⚠️ Vault | ⚠️ Base-only |
| **Multi-Chain (6+)** | ✅ | 1 | Multi | Multi | 1 |
| **MCP Native** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Sovereign Settlement** | ✅ | ❌ | ❌ | ❌ | ❌ |

---

## Links

*   [Documentation](https://docs.rugmunch.io/x402-coffer)
*   [Architecture Deep Dive](ARCHITECTURE.md)
*   [Security Policy](SECURITY.md)
*   [FAQ](FAQ.md)

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://rugmunch.io">Rug Munch Media LLC</a> — sovereign payments for the agentic economy.</sub>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue" /></a>
</p>
