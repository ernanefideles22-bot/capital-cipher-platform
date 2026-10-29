import hashlib
import hmac
import httpx
import pytest
from app.demo.gate_reconcile import inventory, ReconcileError


@pytest.mark.asyncio
async def test_inventory_gets_only_testnet_and_signs_exact_query():
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.method=='GET' and request.url.host=='api-testnet.gateapi.io'
        query=request.url.query.decode()
        digest=hashlib.sha512(b'').hexdigest()
        signed=f'GET\n{request.url.path}\n{query}\n{digest}\n1700000000'
        assert request.headers['SIGN']==hmac.new(b'secret',signed.encode(),hashlib.sha512).hexdigest()
        if request.url.path.endswith('/positions'):
            return httpx.Response(200,json=[{'size':'0'},{'size':'-2'}])
        return httpx.Response(200,json=[{'status':'open'}])
    result=await inventory('key','secret',transport=httpx.MockTransport(handler),clock=lambda:1700000000)
    assert result == {'positions':1,'orders':1,'conditional_orders':1,'atomic':False,'execution_authorized':False}
    assert len(seen)==3


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [
    {'positions':[{'size':'NaN'}]}, {'orders':[{'status':'finished'}]},
    {'stops':[{'status':'open'}]*100}, {'positions':{'error':'invalid'}},
])
async def test_invalid_or_incomplete_inventory_fails_closed(bad):
    def handler(request):
        path=request.url.path
        kind='positions' if path.endswith('/positions') else 'stops' if path.endswith('/price_orders') else 'orders'
        return httpx.Response(200,json=bad.get(kind,[]))
    with pytest.raises(ReconcileError):
        await inventory('key','secret',transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_error_does_not_leak():
    transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'location':'https://other.example'}))
    with pytest.raises(ReconcileError) as exc:
        await inventory('key','secret-private',transport=transport)
    assert 'secret-private' not in str(exc.value)
