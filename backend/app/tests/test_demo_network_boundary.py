"""DEMO egress regressions. Every request terminates in a mock transport."""

import pytest
import httpx

from app.demo.binance import DEMO_ORIGIN, DemoCredentials
from app.demo.venue import FuturesDemoVenue, PUBLIC, SIGNED, VenueError


def demo_venue(handler, *, credentials=True):
    return FuturesDemoVenue(
        DemoCredentials("demo-key-boundary", "mock-signing-secret-boundary") if credentials else None,
        transport=httpx.MockTransport(handler), clock_ms=lambda: 1800000000000,
    )


@pytest.mark.parametrize("method,path,public", [
    *(("GET", path, True) for path in sorted(PUBLIC)),
    *((method, path, False) for method, paths in SIGNED.items() for path in sorted(paths)),
])
async def test_every_allowed_demo_request_uses_exact_https_origin(method, path, public):
    # Compare against an independent literal, not the same imported constant
    # used by the code under test, so changing the origin to production fails.
    assert DEMO_ORIGIN == "https://demo-fapi.binance.com"
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "demo-fapi.binance.com"
        assert request.url.port in (None, 443)
        assert request.url.path == path
        assert request.method == method
        assert ("X-MBX-APIKEY" in request.headers) is (not public)
        assert ("signature" in request.url.params) is (not public)
        return httpx.Response(200, json={})

    venue = demo_venue(handler)
    venue._clock_synchronized = True
    try:
        await venue.request(method, path, {"symbol": "BTCUSDT"}, public=public)
        assert len(seen) == 1
    finally:
        await venue.aclose()


@pytest.mark.parametrize("method,path,public", [
    ("POST", "https://fapi.binance.com/fapi/v1/order", False),
    ("DELETE", "https://demo-fapi.binance.com/fapi/v1/order", False),
    ("GET", "//fapi.binance.com/fapi/v1/order", False),
    ("GET", "/fapi/v1/../v1/order", False),
    ("POST", "/fapi/v1/order?host=fapi.binance.com", False),
    ("PUT", "/fapi/v1/order", False),
    ("POST", "/fapi/v1/order", True),
    ("GET", "/fapi/v1/order", True),
    ("GET", "/fapi/v1/time", False),
])
async def test_demo_host_path_and_method_bypasses_block_before_network(method, path, public):
    def forbidden(request):
        pytest.fail("Blocked request reached the transport")

    venue = demo_venue(forbidden)
    venue._clock_synchronized = True
    try:
        with pytest.raises(VenueError, match="DEMO_ENDPOINT_BLOCKED"):
            await venue.request(method, path, public=public)
    finally:
        await venue.aclose()


@pytest.mark.parametrize("credentials,synchronized", [(False, False), (False, True), (True, False)])
async def test_signed_demo_write_requires_credentials_and_clock_before_network(credentials, synchronized):
    def forbidden(request):
        pytest.fail("Unvalidated signed write reached the transport")

    venue = demo_venue(forbidden, credentials=credentials)
    venue._clock_synchronized = synchronized
    try:
        with pytest.raises(VenueError, match="DEMO_CREDENTIAL_OR_CLOCK_MISSING"):
            await venue.request("POST", "/fapi/v1/order")
    finally:
        await venue.aclose()


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("redirect_status", [301, 302, 303, 307, 308])
async def test_demo_redirect_cannot_forward_signed_request_to_live(method, redirect_status):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "demo-fapi.binance.com"
        return httpx.Response(redirect_status, headers={"Location": "https://fapi.binance.com/fapi/v1/order"})

    venue = demo_venue(handler)
    venue._clock_synchronized = True
    try:
        with pytest.raises(VenueError, match="DEMO_REDIRECT_BLOCKED") as caught:
            await venue.request(method, "/fapi/v1/order")
        assert len(seen) == 1
        assert caught.value.ambiguous is (method != "GET")
    finally:
        await venue.aclose()
