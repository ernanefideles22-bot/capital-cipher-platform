"""Server-only Demo bridge; exchange and connector keys never enter the dashboard bundle."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import success_response

router=APIRouter(prefix='/binance-demo')
ALLOWED_ORIGINS={'http://capital-cipher-binance-demo.railway.internal:8080','https://capital-cipher-binance-demo-staging.up.railway.app'}


async def forward(context, method, path, body=None):
    origin=context.settings.demo_connector_url
    key=context.settings.demo_connector_key
    if origin not in ALLOWED_ORIGINS or not key:
        raise HTTPException(503,'DEMO_CONNECTOR_NOT_CONFIGURED')
    try:
        async with httpx.AsyncClient(base_url=origin,timeout=120 if method=='POST' else 15,follow_redirects=False,trust_env=False) as client:
            response=await client.request(method,path,json=body,headers={'X-API-Key':key})
        if response.is_redirect:
            raise HTTPException(502,'DEMO_CONNECTOR_REDIRECT_BLOCKED')
        data=response.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(502,'DEMO_CONNECTOR_UNAVAILABLE') from None
    if response.status_code>=400:
        detail=data.get('detail','DEMO_CONNECTOR_REQUEST_FAILED') if isinstance(data,dict) else 'DEMO_CONNECTOR_REQUEST_FAILED'
        # Only stable uppercase codes cross this service boundary.
        code=detail if isinstance(detail,str) and detail.replace('_','').isalnum() and detail.isupper() else 'DEMO_CONNECTOR_REQUEST_FAILED'
        raise HTTPException(response.status_code if response.status_code in {401,409,422,503} else 502,code)
    return data


@router.get('/status')
async def status(context:AppContext=Depends(get_context)):
    if not context.settings.demo_connector_url:
        return success_response({'configured':False,'authenticated':False,'automatic_enabled':False})
    return success_response({'configured':True,**await forward(context,'GET','/api/demo/status')})


@router.get('/snapshot',dependencies=[AdminRequired])
async def snapshot(context:AppContext=Depends(get_context)):
    account,execution=await asyncio.gather(forward(context,'GET','/api/demo/account'),forward(context,'GET','/api/demo/execution'))
    return success_response({'account':account,'execution':execution})


class AnalysisBody(BaseModel):
    symbol:Literal['BTCUSDT','ETHUSDT','SOLUSDT']='ETHUSDT'
    timeframe:Literal['1m','5m','15m','1h','4h']='15m'


class ProposalBody(BaseModel):
    proposal_id:str=Field(min_length=8,max_length=100)


class AutomaticBody(BaseModel):
    enabled:bool


@router.post('/analysis',dependencies=[AdminRequired])
async def analyze(body:AnalysisBody,context:AppContext=Depends(get_context)):
    return success_response(await forward(context,'POST','/api/demo/analysis',body.model_dump()))


@router.post('/execute',dependencies=[AdminRequired])
async def execute(body:ProposalBody,context:AppContext=Depends(get_context)):
    if context.risk_manager.kill_switch_active:
        raise HTTPException(409,'CENTRAL_KILL_SWITCH_ACTIVE')
    return success_response(await forward(context,'POST','/api/demo/execute',body.model_dump()))


@router.post('/close',dependencies=[AdminRequired])
async def close(body:ProposalBody,context:AppContext=Depends(get_context)):
    return success_response(await forward(context,'POST','/api/demo/close',body.model_dump()))


@router.post('/automatic',dependencies=[AdminRequired])
async def automatic(body:AutomaticBody,context:AppContext=Depends(get_context)):
    if body.enabled and context.risk_manager.kill_switch_active:
        raise HTTPException(409,'CENTRAL_KILL_SWITCH_ACTIVE')
    return success_response(await forward(context,'POST','/api/demo/automatic',body.model_dump()))


@router.post('/kill',dependencies=[AdminRequired])
async def kill(context:AppContext=Depends(get_context)):
    return success_response(await forward(context,'POST','/api/demo/kill'))


@router.post('/reconcile',dependencies=[AdminRequired])
async def reconcile(context:AppContext=Depends(get_context)):
    return success_response(await forward(context,'POST','/api/demo/reconcile'))


@router.post('/reset',dependencies=[AdminRequired])
async def reset(context:AppContext=Depends(get_context)):
    return success_response(await forward(context,'POST','/api/demo/reset'))


@router.post('/canary',dependencies=[AdminRequired])
async def canary(context:AppContext=Depends(get_context)):
    if context.risk_manager.kill_switch_active:
        raise HTTPException(409,'CENTRAL_KILL_SWITCH_ACTIVE')
    return success_response(await forward(context,'POST','/api/demo/canary'))
