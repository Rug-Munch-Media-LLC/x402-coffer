"""coffer_client — client-side x402 payment SDK.

The ``x402-reqwest``/``fetch`` pattern for Python: wrap httpx so a paid
endpoint Just Works —

    client = CofferClient(wallet_key=KEY, rpc_url=RPC)
    r = await client.get("https://api.example.com/paid")   # auto-pays on 402

Flow on a 402: parse ``accepts[]`` -> pick the option matching the configured
network/asset -> send the on-chain payment -> ``POST /v1/x402/verify`` ->
retry the original request with ``X-Payment-ID``.

Honest by construction: payments are only sent when a funded key + RPC are
configured; ``dry_run=True`` stops right before broadcasting (returns the
would-be payment for inspection).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

__all__ = ["CofferClient", "PaymentDecision"]


@dataclass
class PaymentDecision:
    """What the client decided to do about a 402."""

    network: str
    asset: str
    pay_to_address: str
    max_amount_required: str  # atomic units, string per spec
    tx_hash: str | None = None  # set once broadcast (None under dry_run)
    dry_run: bool = False
    note: str = ""


@dataclass
class _WalletConfig:
    key: Any = None  # eth_account LocalAccount
    address: str = ""
    rpc_url: str = ""
    network: str = "eip155:8453"
    asset: str = "USDC"


class CofferClient:
    """httpx client wrapper with transparent x402 payment handling."""

    def __init__(
        self,
        *,
        wallet_key: str | None = None,
        rpc_url: str = "",
        network: str = "eip155:8453",
        asset: str = "USDC",
        timeout: float = 30.0,
        max_payment_usd_atomic: int | None = None,
        dry_run: bool = False,
        payment_sender: Any = None,
        base_headers: dict[str, str] | None = None,
    ) -> None:
        """``payment_sender`` injects a custom broadcaster (tests / alt chains):
        ``async f(pay_to: str, amount_atomic: int, network: str, asset: str) -> tx_hash``.
        ``max_payment_usd_atomic`` is a hard spending ceiling — offers above it
        are refused instead of paid."""
        self._wallet = _WalletConfig()
        self.dry_run = bool(dry_run)
        self.max_payment_atomic = max_payment_usd_atomic
        self._sender = payment_sender
        self._headers = dict(base_headers or {})
        if wallet_key:
            try:
                from eth_account import Account

                self._wallet.key = Account.from_key(wallet_key)
                self._wallet.address = self._wallet.key.address
            except Exception as e:  # noqa: BLE001 — fail closed on bad key
                raise ValueError(f"invalid wallet_key: {e}") from e
            self._wallet.rpc_url = rpc_url
            self._wallet.network = network
            self._wallet.asset = asset
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── public surface ──────────────────────────────────────────────────

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def request(
        self, method: str, url: str, *, max_attempts: int = 2, **kw: Any
    ) -> httpx.Response:
        headers = {**self._headers, **(kw.pop("headers", {}) or {})}
        resp = await self._http.request(method, url, headers=headers, **kw)
        attempts = 1
        while resp.status_code == 402 and attempts < max_attempts:
            decision = await self.handle_402(resp)
            if decision is None:
                return resp  # nothing payable we're willing to do
            if decision.tx_hash is None:
                # dry_run or refused — attach the decision for the caller.
                resp.headers["x-coffer-decision"] = decision.note or (
                    "dry-run" if decision.dry_run else "refused"
                )
                return resp
            payment_id = await self._verify_payment(decision, url)
            if not payment_id:
                return resp
            headers["X-Payment-ID"] = payment_id
            resp = await self._http.request(method, url, headers=headers, **kw)
            attempts += 1
        return resp

    # ── 402 handling ────────────────────────────────────────────────────

    async def handle_402(self, resp: httpx.Response) -> PaymentDecision | None:
        """Parse a 402, pick our option, pay. None => nothing we can pay."""
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        accepts = body.get("accepts") or []
        option = self._pick_option(accepts)
        if option is None:
            return None
        amount_atomic = int(str(option.get("maxAmountRequired", "0")))
        if self.max_payment_atomic is not None and amount_atomic > self.max_payment_atomic:
            log.warning("coffer_client_refused_over_ceiling", extra={"amount": amount_atomic})
            return PaymentDecision(
                network=str(option.get("network", "")),
                asset=str(option.get("asset", "")),
                pay_to_address=str(option.get("payToAddress", "")),
                max_amount_required=str(amount_atomic),
                note="over spending ceiling",
            )
        if self._wallet.key is None:
            log.info("coffer_client_no_wallet_configured")
            return PaymentDecision(
                network=str(option.get("network", "")),
                asset=str(option.get("asset", "")),
                pay_to_address=str(option.get("payToAddress", "")),
                max_amount_required=str(amount_atomic),
                note="no wallet configured",
            )
        tx_hash = await self._broadcast(option, amount_atomic)
        return PaymentDecision(
            network=str(option.get("network", "")),
            asset=str(option.get("asset", "")),
            pay_to_address=str(option.get("payToAddress", "")),
            max_amount_required=str(amount_atomic),
            tx_hash=tx_hash,
            dry_run=self.dry_run,
            note="dry-run" if self.dry_run else "",
        )

    def _pick_option(self, accepts: list[dict[str, Any]]) -> dict[str, Any] | None:
        for opt in accepts:
            if (
                str(opt.get("network", "")).lower() == self._wallet.network.lower()
                and str(opt.get("asset", "")).upper() == self._wallet.asset.upper()
            ):
                return opt
        return None

    async def _broadcast(self, option: dict[str, Any], amount_atomic: int) -> str | None:
        """Send the on-chain payment. Returns tx_hash, or None on refusal/dry-run."""
        pay_to = str(option.get("payToAddress", ""))
        network = str(option.get("network", ""))
        asset = str(option.get("asset", ""))
        if self.dry_run:
            log.info("coffer_client_dry_run", extra={"pay_to": pay_to, "amount": amount_atomic})
            return None
        if self._sender is not None:
            return await self._sender(pay_to, amount_atomic, network, asset)
        return await self._evm_transfer(pay_to, amount_atomic, asset)

    async def _evm_transfer(self, pay_to: str, amount_atomic: int, asset: str) -> str | None:
        """Broadcast an EVM ERC-20 transfer via the configured RPC. Fail-closed."""
        if self._wallet.key is None or not self._wallet.rpc_url:
            log.error("coffer_client_missing_rpc_or_key")
            return None
        try:
            from eth_utils.crypto import keccak

            from coffer_client._erc20 import encode_erc20_transfer, erc20_asset_address
        except ImportError:
            from ._erc20 import encode_erc20_transfer, erc20_asset_address  # type: ignore[no-redef]

        token = erc20_asset_address(asset, self._wallet.network)
        if token is None:
            log.error("coffer_client_unknown_asset", extra={"asset": asset})
            return None
        data = encode_erc20_transfer(pay_to, amount_atomic)
        selector = keccak(b"transfer(address,uint256)")[:4].hex()
        async with httpx.AsyncClient(timeout=30.0) as rpc:

            async def call(method: str, params: list[Any]) -> Any:
                r = await rpc.post(
                    self._wallet.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                r.raise_for_status()
                out = r.json()
                if "error" in out:
                    raise RuntimeError(f"RPC error: {out['error']}")
                return out["result"]

            nonce = int(
                await call("eth_getTransactionCount", [self._wallet.address, "pending"]), 16
            )
            gas_price = int(await call("eth_gasPrice", []), 16)
            token_acct = await call("eth_getTransactionCount", [token, "latest"])  # liveness probe
            del token_acct
            tx: dict[str, Any] = {
                "to": token,
                "value": 0,
                "gas": 80_000,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": int(self._wallet.network.split(":")[1]),
                "data": "0x" + selector + data,
            }
            signed = self._wallet.key.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            tx_hash = await call(
                "eth_sendRawTransaction",
                ["0x" + (raw.hex() if isinstance(raw, bytes) else str(raw))],
            )
            log.info("coffer_client_broadcast", extra={"tx": tx_hash})
            return str(tx_hash)

    async def _verify_payment(self, decision: PaymentDecision, resource_url: str) -> str | None:
        """POST /v1/x402/verify -> verified payment id for replay."""
        u = httpx.URL(resource_url)
        port = f":{u.port}" if u.port else ""
        verify_url = f"{u.scheme}://{u.host}{port}/v1/x402/verify"
        payload = {
            "paymentId": f"auto-{decision.tx_hash}",
            "transactionHash": decision.tx_hash,
            "network": decision.network,
            "asset": decision.asset,
            "amount": decision.max_amount_required,
            "scheme": "exact",
        }
        try:
            r = await self._http.post(verify_url, json=payload)
            r.raise_for_status()
            out = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("coffer_client_verify_failed", extra={"error": str(e)})
            return None
        if not (out.get("verified") or out.get("isValid")):
            log.warning("coffer_client_not_verified", extra={"reason": out.get("reason", "")})
            return None
        pid = out.get("payment_id") or out.get("paymentId") or payload["paymentId"]
        return str(pid)


async def spend_ceiling_guard(client: CofferClient, ceiling_atomic: int) -> None:
    """Utility: raise the client's spending ceiling guard explicitly."""
    client.max_payment_atomic = ceiling_atomic
    await asyncio.sleep(0)
