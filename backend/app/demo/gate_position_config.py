"""Read BTC/USDT position mode and margin configuration from Gate Testnet."""
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import time
import httpx

_HOST='https://api-testnet.gateapi.io'
_PATH='/api/v4/futures/usdt/positions/BTC_USDT'


class ConfigUnavailable(Exception):
    pass


def interpret_position_config(row, *, contract='BTC_USDT'):
    if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'} or not isinstance(row,dict) or row.get('contract')!=contract:
        raise ConfigUnavailable('invalid contract')
    try:
        mode=row['mode']
        if mode not in {'single','dual_long','dual_short'}:
            raise ValueError()
        size=Decimal(str(row['size']))
        legacy=Decimal(str(row['leverage']))
        if not size.is_finite() or not legacy.is_finite() or legacy < 0:
            raise ValueError()
        explicit=row.get('pos_margin_mode')
        if explicit not in (None,'','isolated','cross'):
            raise ValueError()
        margin=explicit if explicit in {'isolated','cross'} else ('cross' if legacy==0 else 'isolated')
        lever=row.get('lever')
        if lever not in (None,''):
            effective=Decimal(str(lever))
        else:
            effective=legacy if margin=='isolated' else Decimal(str(row['cross_leverage_limit']))
        if not effective.is_finite() or effective <= 0:
            raise ValueError()
        if explicit=='isolated' and (legacy<=0 or (lever not in (None,'') and effective!=legacy)):
            raise ValueError()
        if explicit=='cross' and legacy>0:
            raise ValueError()
        return {'mode':mode,'margin':margin,'leverage':str(effective),'position_open':size != 0,
                'one_way_isolated_1x':mode=='single' and margin=='isolated' and effective==1,
                'execution_authorized':False}
    except (KeyError,TypeError,ValueError,InvalidOperation):
        raise ConfigUnavailable('ambiguous position configuration') from None


async def read_position_config(key,secret,*,transport=None,clock=time.time):
    if not key or not secret:
        raise ConfigUnavailable('credentials missing')
    timestamp=str(int(clock()))
    digest=hashlib.sha512(b'').hexdigest()
    signature=hmac.new(secret.encode(),f'GET\n{_PATH}\n\n{digest}\n{timestamp}'.encode(),hashlib.sha512).hexdigest()
    try:
        async with httpx.AsyncClient(transport=transport,timeout=8,trust_env=False,follow_redirects=False) as client:
            response=await client.get(_HOST+_PATH,headers={'KEY':key,'Timestamp':timestamp,'SIGN':signature})
        if response.status_code!=200:
            raise ConfigUnavailable('read unavailable')
        return interpret_position_config(response.json())
    except (httpx.HTTPError,ValueError,TypeError):
        raise ConfigUnavailable('read unavailable') from None
