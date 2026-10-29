import hashlib
import hmac
import io
import urllib.error

import pytest

from app.demo import gate_readonly


def test_probe_signs_only_testnet_account_get(monkeypatch):
    monkeypatch.setattr(gate_readonly.time, "time", lambda: 1700000000)

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://api-testnet.gateapi.io/api/v4/futures/usdt/accounts"
            assert request.method == "GET"
            assert timeout == 10
            digest = hashlib.sha512(b"").hexdigest()
            message = f"GET\n/api/v4/futures/usdt/accounts\n\n{digest}\n1700000000"
            signature = hmac.new(b"secret", message.encode(), hashlib.sha512).hexdigest()
            assert request.get_header("Sign") == signature
            return io.BytesIO(b'{"currency":"USDT","total":"1000","other":"not exposed"}')

    monkeypatch.setattr(gate_readonly.urllib.request, "build_opener", lambda *handlers: Opener())
    assert gate_readonly.probe_account("key", "secret") == {
        "environment": "gate_testnet", "authenticated": True, "currency": "USDT", "total": "1000"
    }


def test_probe_rejects_redirect_and_sanitizes_errors(monkeypatch):
    handler = gate_readonly._NoRedirect()
    assert handler.redirect_request(None, None, 302, "", {}, "https://example.org") is None

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError("secret-value")

    monkeypatch.setattr(gate_readonly.urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(gate_readonly.GateProbeError, match="Falha ao consultar") as error:
        gate_readonly.probe_account("key", "secret-value")
    assert "secret-value" not in str(error.value)


def test_probe_rejects_unexpected_response(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'{"message":"error"}')

    monkeypatch.setattr(gate_readonly.urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(gate_readonly.GateProbeError, match="Resposta inesperada"):
        gate_readonly.probe_account("key", "secret")
