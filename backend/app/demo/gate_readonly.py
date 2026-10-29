"""Read-only connectivity probe for Gate's USDT futures Testnet.

This module does not implement order execution. It deliberately has no
configurable URL or generic request method.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

_HOST = "https://api-testnet.gateapi.io"
_PATH = "/api/v4/futures/usdt/accounts"


class GateProbeError(Exception):
    """The connection or authentication could not be validated."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_account(key: str, secret: str) -> dict:
    """Query the demo futures account; credentials must be Testnet keys."""
    if not key or not secret:
        raise GateProbeError("Credenciais Testnet ausentes")
    timestamp = str(int(time.time()))
    digest = hashlib.sha512(b"").hexdigest()
    signature = hmac.new(
        secret.encode(), f"GET\n{_PATH}\n\n{digest}\n{timestamp}".encode(), hashlib.sha512
    ).hexdigest()
    request = urllib.request.Request(
        _HOST + _PATH,
        headers={"KEY": key, "Timestamp": timestamp, "SIGN": signature, "Accept": "application/json"},
        method="GET",
    )
    try:
        # Reject redirects so credentials cannot be forwarded to another host.
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GateProbeError("Falha ao consultar a Gate Testnet") from None
    if not isinstance(body, dict) or "currency" not in body or "total" not in body:
        raise GateProbeError("Resposta inesperada da Gate Testnet")
    return {"environment": "gate_testnet", "authenticated": True,
            "currency": body["currency"], "total": body["total"]}
