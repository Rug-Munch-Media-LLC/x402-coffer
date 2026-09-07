"""Tests for Flask (WSGI) + Litestar adapters."""

from __future__ import annotations

import json

from coffer._engine.adapters_wsgi import Flask402Gate, init_flask, litestar_middleware
from coffer._engine.store import PaymentStore

PAY_TO = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"


class _FakeFlaskApp:
    """Minimal WSGI app standing in for Flask."""

    def __init__(self) -> None:
        self.wsgi_app = self._serve

    def _serve(self, environ, start_response):
        body = b'{"ok": true}'
        start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        return [body]


def _environ(path: str = "/paid", payment_id: str = "", remote_addr: str = "1.2.3.4") -> dict:
    return {
        "PATH_INFO": path,
        "HTTP_X_PAYMENT_ID": payment_id,
        "REMOTE_ADDR": remote_addr,
        "HTTP_HOST": "api.test",
        "wsgi.url_scheme": "http",
        "REQUEST_METHOD": "GET",
    }


def _run(gate: Flask402Gate, environ: dict) -> tuple[int, dict, bytes]:
    status_headers: list = []

    def start_response(status, headers):
        status_headers.append((status, headers))

    body = b"".join(gate(environ, start_response))
    status, headers = status_headers[0]
    hdrs = {k.lower(): v for k, v in headers}
    return int(status.split()[0]), hdrs, body


def test_unpaid_request_gets_spec_402() -> None:
    gate = Flask402Gate(_FakeFlaskApp().wsgi_app, pay_to_address=PAY_TO)
    code, headers, body = _run(gate, _environ())
    assert code == 402
    assert headers["x-payment-required"] == "true"
    assert headers["x-payment-options"] == "x402"
    assert "payment-required" in headers
    parsed = json.loads(body)
    # The gate advertises the FACILITATOR'S configured wallet (env default).
    from coffer._engine.facilitator_config import X402_WALLET

    assert parsed["accepts"][0]["payToAddress"].lower() == X402_WALLET.lower()
    assert isinstance(parsed["accepts"][0]["maxAmountRequired"], str)


def test_valid_payment_id_passes_through() -> None:
    store = PaymentStore()
    gate = Flask402Gate(_FakeFlaskApp().wsgi_app, store=store, pay_to_address=PAY_TO)
    store.record(payment_id="pid-1", caller_key="1.2.3.4", amount_usd=0.01)
    code, _, body = _run(gate, _environ(payment_id="pid-1"))
    assert code == 200
    assert json.loads(body)["ok"] is True


def test_wrong_caller_payment_id_rejected() -> None:
    store = PaymentStore()
    gate = Flask402Gate(_FakeFlaskApp().wsgi_app, store=store, pay_to_address=PAY_TO)
    store.record(payment_id="pid-2", caller_key="5.6.7.8", amount_usd=0.01)
    code, _, _ = _run(gate, _environ(payment_id="pid-2", remote_addr="1.2.3.4"))
    assert code == 402  # caller-bound replay enforced over WSGI too


def test_free_paths_pass_and_empty_prefix_ignored() -> None:
    gate = Flask402Gate(_FakeFlaskApp().wsgi_app, free_paths={"/health", ""}, pay_to_address=PAY_TO)
    code, _, _ = _run(gate, _environ(path="/health"))
    assert code == 200
    code, _, _ = _run(gate, _environ(path="/anything"))  # "" must NOT match-all
    assert code == 402


def test_init_flask_wraps_in_place() -> None:
    app = _FakeFlaskApp()
    gate = init_flask(app, pay_to_address=PAY_TO, free_paths={"/health"})
    assert isinstance(gate, Flask402Gate)
    code, _, _ = _run(gate, _environ())  # wrapped wsgi_app gates
    assert code == 402


def test_litestar_middleware_factory_builds_gate() -> None:
    from coffer._engine.adapters import X402Middleware

    factory = litestar_middleware(free_paths={"/health"})
    mw = factory(_FakeFlaskApp())  # framework calls the factory with the app
    assert isinstance(mw, X402Middleware)
    assert mw.free_paths == {"/health"}
