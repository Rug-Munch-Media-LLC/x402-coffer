# Integration Guide

How to wire a resource server (or a paying agent) to Coffer.

## Merchant Side: Protect a Resource Server

### 1. Install

```bash
pip install x402-coffer
```

### 2. Configure the receiving wallet

```bash
export X402_WALLET="0xYourEVMAddress"
# Solana: export X402_WALLET_SOLANA="YourSolanaAddress"
# TRON:   export X402_WALLET_TRON="YourTronAddress"
export X402_NETWORK="base"   # default network
```

A misconfigured merchant wallet fails fast at startup with a clear error — Coffer never silently accepts payments it cannot route.

### 3. Add the payment gate

```python
from coffer import require_payment

@app.get("/premium-tool")
@require_payment(asset="USDC", network="base", amount=0.001, operation="tool_use")
async def premium_endpoint(request):
    return {"status": "success", "data": "Access granted!"}
```

The decorator handles the full x402 flow: it returns the `HTTP 402` quote, verifies the retried payment, settles idempotently, and stamps the response with `PAYMENT-RESPONSE` and `X-Payment-ID`.

### 4. Optional: external facilitator routing

If you prefer not to self-settle, point Coffer at an external facilitator:

```bash
export X402_FACILITATOR_URL="https://facilitator.example.com"
```

## Payer Side: Agents Consuming Paid Resources

### External facilitator (consumer mode)

Point your client library at a facilitator endpoint:

```bash
export X402_FACILITATOR_URL="https://your-coffer.example.com"
```

### Self-facilitator signing (payer mode)

For autonomous agents that sign their own payments without a custodian:

```bash
export X402_SELF_FACILITATOR_KEY="<signing key>"        # or, preferred:
export X402_SELF_FACILITATOR_KEY_FILE="/path/to/key"
```

The key must come from the environment or a secret file — never from code. See [security-model.md](security-model.md) for key-handling guidance.

## Real-World Consumer Examples

Two production consumers in the Rug Munch ecosystem demonstrate the integration patterns:

- **`rmi-mcp-x402`** — `app/domain/x402/facilitator_client.py`: an MCP server acting as a payer, using a facilitator client to complete x402 flows for paid tool calls.
- **`walletpress`** — `backend/coffer_client.py`: a backend service integrating with Coffer's facilitator API for wallet-funded payments.

Both are reference implementations for the client side of the quote → verify → settle flow.

## Billing Surface

Once payments flow, Coffer exposes a customer billing view backed by its JSONL ledger:

- `GET /v1/billing/history` — settlement history for the authenticated caller (`{"transactions": [...], "total_spent": ...}`)
- `GET /v1/billing/balance` — prepaid balance view (`{"balance_usd", "balance_by_chain", "last_settlement"}`)

Attribution is bound to the verified payment's caller key. A client-supplied `X-User-Id` header is never trusted — it is trivially spoofable. Deployments fronting Coffer with an auth proxy should terminate caller identity at the proxy.

## Health and Operations

- `GET /health` — liveness endpoint for orchestrators and load balancers.
- Rate limiting and spending policy are enforced per caller before any chain contact; denials return RFC 7807 `429` problems with a `retry_after` hint.

See [x402-spec-compliance.md](x402-spec-compliance.md) for the protocol flow and [security-model.md](security-model.md) for the threat model.
