"""S2D paged public OHLCV and explicitly immature frozen-forward results."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_s2d_history import (
    GateMarketDataError, fetch_gate_historical_range,
)
from app.strategy.gate_s2d_forward import (
    FROZEN_AT, POLICY_SOURCE_COMMIT, evaluate_frozen_forward,
)

UTC=timezone.utc
STEP={"15m":900,"1h":3600,"4h":14400}


def candle_series(tf,latest,n,*,symbol="BTCUSDT",receipt=None):
    step=timedelta(seconds=STEP[tf])
    end=int(latest.timestamp())
    end-=end%STEP[tf]
    output=[]
    for i in range(n):
        closed=datetime.fromtimestamp(end-(n-i-1)*STEP[tf],UTC)
        level=100+.02*i
        output.append(Candle(
            exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
            open=level,high=level+1,low=level-1,close=level+.1,
            volume=100,closed_at=closed,
            received_at=receipt or closed+timedelta(microseconds=1),
        ))
    return output


def make_frames(latest,*,count=350,receipt=None):
    return {
        "15m":candle_series("15m",latest,count,receipt=receipt),
        "1h":candle_series("1h",latest,170,receipt=receipt),
        "4h":candle_series("4h",latest,170,receipt=receipt),
    }


@pytest.mark.asyncio
async def test_gate_historical_range_is_public_paged_and_exact_no_limit():
    latest=datetime(2026,11,1,12,tzinfo=UTC)
    n=1201
    first=latest-timedelta(minutes=15*(n-1))
    requests=[]
    def handler(request):
        assert request.method=="GET"
        assert request.url.host=="api-testnet.gateapi.io"
        if request.url.path.endswith("/contracts/BTC_USDT"):
            return httpx.Response(200,json={
                "name":"BTC_USDT","quanto_multiplier":"0.01"})
        query=parse_qs(request.url.query.decode())
        assert "limit" not in query
        requests.append(query)
        begin=int(query["from"][0])
        until=int(query["to"][0])
        assert (until-begin)//900+1<=500
        bars=[]
        for start in range(begin,until+1,900):
            bars.append({
                "t":start,"o":"100","h":"102","l":"98",
                "c":"101","v":"5",
            })
        return httpx.Response(200,json=list(reversed(bars)))
    result=await fetch_gate_historical_range(
        "BTCUSDT","15m",first_closed_at=first,last_closed_at=latest,
        transport=httpx.MockTransport(handler),
        clock=lambda:latest.timestamp()+10,
    )
    assert len(result)==n
    assert len(requests)==3
    assert result[0]["close_time_ms"]==int(first.timestamp()*1000)
    assert result[-1]["close_time_ms"]==int(latest.timestamp()*1000)
    assert result[0]["volume"]==pytest.approx(.05)


@pytest.mark.asyncio
async def test_paged_missing_candle_is_rejected_not_silently_filled():
    latest=datetime(2026,11,1,12,tzinfo=UTC)
    def handler(request):
        if "/contracts/" in request.url.path:
            return httpx.Response(200,json={
                "name":"BTC_USDT","quanto_multiplier":"1"})
        query=parse_qs(request.url.query.decode())
        start=int(query["from"][0])
        final=int(query["to"][0])
        return httpx.Response(200,json=[
            {"t":t,"o":"100","h":"101","l":"99","c":"100","v":"10"}
            for t in range(start,final+1,900) if t!=start+900
        ])
    with pytest.raises(GateMarketDataError):
        await fetch_gate_historical_range(
            "BTCUSDT","15m",
            first_closed_at=latest-timedelta(minutes=15*99),
            last_closed_at=latest,transport=httpx.MockTransport(handler),
            clock=lambda:latest.timestamp()+30,
        )


@pytest.mark.asyncio
async def test_future_and_unbounded_historical_window_fail_before_network():
    latest=datetime(2026,11,1,12,tzinfo=UTC)
    with pytest.raises(ValueError,match="Future"):
        await fetch_gate_historical_range(
            "BTCUSDT","15m",first_closed_at=latest,
            last_closed_at=latest,
            clock=lambda:latest.timestamp()-1,
        )
    with pytest.raises(ValueError,match="exceeds bounded"):
        await fetch_gate_historical_range(
            "BTCUSDT","15m",
            first_closed_at=latest-timedelta(minutes=15*6500),
            last_closed_at=latest,clock=lambda:latest.timestamp()+1,
        )


def test_frozen_window_before_policy_close_never_claims_profit():
    last=datetime(2026,11,1,23,45,tzinfo=UTC)
    groups=make_frames(last)
    result=evaluate_frozen_forward(
        groups,observed_at=last+timedelta(seconds=3))
    assert result.policy_source_commit==POLICY_SOURCE_COMMIT
    assert result.status=="PRECOMMITTED_WINDOW_NOT_STARTED"
    assert result.comparisons==()
    assert result.order_submission_authorized is False
    assert result.profitability_proven is False


def test_post_freeze_immaturity_is_explicit_and_cannot_emit_orders():
    latest=FROZEN_AT+timedelta(minutes=15*20)
    groups=make_frames(latest)
    result=evaluate_frozen_forward(
        groups,observed_at=latest+timedelta(seconds=4))
    assert result.status=="INSUFFICIENT_NEW_CLOSED_BARS"
    assert result.post_freeze_bars==20
    assert not result.automatic_promotion_authorized
    assert not result.order_submission_authorized


def test_forward_engine_compares_frozen_strategies_without_auth(monkeypatch):
    from app.strategy import gate_s2_replay as replay
    from app.strategy import gate_s2c_confluence as confluence
    from app.strategy.gate_classical_s2 import ResearchPlan
    def never(strategy,bars,*,as_of,historical_replay):
        assert historical_replay is True
        return ResearchPlan(
            strategy,"1.0.0",bars[-1].symbol,bars[-1].timeframe,
            as_of,"WAIT",None,None,"NO_EDGE",False,False)
    monkeypatch.setattr(replay,"evaluate_strategy",never)
    monkeypatch.setattr(confluence,"evaluate_strategy",never)
    latest=FROZEN_AT+timedelta(minutes=15*120)
    grouped=make_frames(latest,count=350)
    r=evaluate_frozen_forward(grouped,observed_at=latest+timedelta(seconds=2))
    assert r.status=="INSUFFICIENT_FORWARD_TRADES"
    assert r.post_freeze_bars==120
    assert len(r.comparisons)==3
    assert all(
        x.filtered_trades==x.baseline_trades==0 and x.filtered_net_pnl==0
        and x.economic_edge_proven is False
        for x in r.comparisons
    )
    assert not r.profitability_proven


def test_future_policy_requires_pre_freeze_history_and_provenance():
    latest=FROZEN_AT+timedelta(minutes=15*160)
    grouped=make_frames(latest,count=165)
    r=evaluate_frozen_forward(grouped,observed_at=latest+timedelta(seconds=2))
    assert r.status=="PRE_FREEZE_WARMUP_EVICTED_OR_MISSING"
    assert r.comparisons==()
    groups=make_frames(latest,count=350)
    groups["1h"][3]=groups["1h"][3].model_copy(update={"symbol":"SOLUSDT"})
    with pytest.raises(ValueError,match="Mismatched"):
        evaluate_frozen_forward(groups,observed_at=latest+timedelta(seconds=2))
