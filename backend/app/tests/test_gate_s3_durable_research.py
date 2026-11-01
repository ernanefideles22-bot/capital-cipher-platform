"""Durable S3 research replay, hash-chain failures and no-OMS security gates."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2e_live_shadow import S2E_POLICY,S2E_POLICY_COMMIT
from app.strategy.gate_s3_durable_research import (
    DurableS3ForwardResearch,S3JournalBlocked,
)
from app.strategy.gate_s2_replay import Friction

UTC=timezone.utc
T0=datetime(2026,11,2,0,15,tzinfo=UTC)
SELECTED="DONCHIAN_VOLUME_BREAKOUT_V1"


def candle(i,*,symbol="BTCUSDT",high=101.,low=99.,open_price=100.):
    ts=T0+timedelta(minutes=15*i)
    return Candle(
        exchange=Exchange.GATE,symbol=symbol,timeframe="15m",
        open=open_price,high=high,low=low,close=100.,
        volume=100,closed_at=ts,received_at=ts+timedelta(seconds=5))


def shadow(bar,*,action="WAIT",filtered=None,extra=None):
    if filtered is None:filtered=action
    rows=[]
    for name in sorted(STRATEGIES):
        proposed=action if name==SELECTED else "WAIT"
        gated=filtered if name==SELECTED else "WAIT"
        rows.append({
            "strategy_id":name,"strategy_version":STRATEGIES[name].version,
            "timeframe":"15m","proposed_action":proposed,
            "filtered_action":gated,
            "rejection_reasons":["HIGHER_TIMEFRAMES_NOT_ALIGNED"]
                if proposed!="WAIT" and gated=="WAIT" else [],
            "stop_distance":2. if proposed!="WAIT" else None,
            "target_distance":4. if proposed!="WAIT" else None,
            "execution_authorized":False,"predictive_edge_proven":False,
        })
    result={
        "status":"READ_ONLY_CONFLUENCE_READY",
        "symbol":bar.symbol,
        "as_of_utc":bar.received_at.isoformat(),
        "policy_id":S2E_POLICY,"policy_commit":S2E_POLICY_COMMIT,
        "oms_touched":False,"source_decision_cohort_modified":False,
        "execution_authorized":False,"hypotheses":rows,
    }
    result.update(extra or {})
    return result


def send(journal,index,*,symbol="BTCUSDT",action="WAIT",filtered=None,
         high=101.,low=99.,extra=None):
    bar=candle(index,symbol=symbol,high=high,low=low)
    return journal.record(bar,shadow(bar,action=action,filtered=filtered,
                                    extra=extra),observed_at=bar.received_at)


def research_account(report,arm):
    return next(b for b in report["accounts"]
                if b["strategy"]==SELECTED and b["arm"]==arm)


def test_restart_replays_exact_research_pnl_and_open_positions(tmp_path):
    path=tmp_path/"s3-forward-only.sqlite3"
    with DurableS3ForwardResearch(path,friction=Friction(0,0,0)) as d:
        send(d,0,action="LONG",filtered="WAIT")
        send(d,1)
        prior=send(d,2,high=101,low=99)
        assert research_account(prior,"STANDALONE")["open_position"]
        assert not research_account(prior,"S1_CONFLUENCE")["open_position"]
        status=d.status()
        assert status["observations"]==3 and status["execution_authorized"] is False
        assert not status["remote_durability_proven"]
    with DurableS3ForwardResearch(path,friction=Friction(0,0,0)) as restored:
        assert restored.scorer.snapshot("BTCUSDT")==prior
        result=send(restored,3,high=105,low=99)
        assert research_account(result,"STANDALONE")["completed_trades"]==1
        assert research_account(result,"STANDALONE")["net_pnl"]>0
        assert research_account(result,"S1_CONFLUENCE")["net_pnl"]==0
        assert restored.status()["observations"]==4
    with DurableS3ForwardResearch(path,friction=Friction(0,0,0)) as final:
        assert final.scorer.snapshot("BTCUSDT")==result
        assert final.status()["observations"]==4


def test_same_observation_retries_idempotently_across_restarts(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        first=send(d,0,action="LONG",filtered="WAIT")
        again=send(d,0,action="LONG",filtered="WAIT")
        assert again["duplicate"] and d.status()["observations"]==1
    with DurableS3ForwardResearch(path) as d:
        again=send(d,0,action="LONG",filtered="WAIT")
        assert again["duplicate"] and d.status()["observations"]==1
        with pytest.raises(S3JournalBlocked,match="Conflicting"):
            send(d,0,action="SHORT",filtered="WAIT")
        assert d.status()["observations"]==1


def test_gap_is_not_persisted_or_silently_repaired_after_restart(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0)
        with pytest.raises(S3JournalBlocked,match="discontinuous"):
            send(d,2)
        assert d.status()["observations"]==1
    with DurableS3ForwardResearch(path) as d:
        with pytest.raises(S3JournalBlocked,match="discontinuous"):
            send(d,2)
        assert d.status()["observations"]==1


def test_research_only_whitelist_never_persists_arbitrary_secrets(tmp_path):
    path=tmp_path/"public.db"
    forbidden="NEVER_PERSIST_GATE_TESTNET_SECRET"
    with DurableS3ForwardResearch(path) as d:
        bar=candle(0)
        public=shadow(bar,action="LONG",extra={
            "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET":forbidden})
        public["hypotheses"][0]["private_api_key"]=forbidden
        d.record(bar,public,observed_at=bar.received_at)
        payload=d.db.execute(
            "SELECT payload_json FROM s3_observations").fetchone()[0]
        assert forbidden not in payload
        assert d.status()["oms_touched"] is False
        bad=shadow(candle(1),action="LONG",
                   extra={"execution_authorized":True})
        with pytest.raises(S3JournalBlocked,match="unsafe"):
            d.record(candle(1),bad,observed_at=candle(1).received_at)
        assert d.status()["observations"]==1


def test_append_only_events_cannot_update_or_delete(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0)
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            d.db.execute("UPDATE s3_observations SET symbol='ETHUSDT'")
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            d.db.execute("DELETE FROM s3_observations")
        assert d.status()["observations"]==1


def test_tampered_hash_chain_fails_closed_at_recovery(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0,action="LONG")
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER s3_no_event_update")
        db.execute("UPDATE s3_observations SET payload_json='{}'")
        db.commit()
    with pytest.raises(S3JournalBlocked,match="hash chain"):
        DurableS3ForwardResearch(path)


def test_missing_suffix_detected_by_head_record(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0)
        send(d,1)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER s3_no_event_delete")
        db.execute("DELETE FROM s3_observations WHERE seq=2")
        db.commit()
    with pytest.raises(S3JournalBlocked,match="head/row mismatch"):
        DurableS3ForwardResearch(path)


def test_two_writers_must_fail_closed_and_restart(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as first:
        with DurableS3ForwardResearch(path) as stale:
            send(first,0)
            with pytest.raises(S3JournalBlocked,match="Concurrent"):
                send(stale,1)
        assert first.status()["observations"]==1
    with DurableS3ForwardResearch(path) as fresh:
        send(fresh,1)
        assert fresh.status()["observations"]==2


def test_multiple_markets_replay_separately_without_agent_registry(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0,symbol="BTCUSDT",action="LONG",filtered="WAIT")
        send(d,0,symbol="ETHUSDT",action="WAIT")
        send(d,1,symbol="BTCUSDT")
        send(d,1,symbol="ETHUSDT")
        before_btc=d.scorer.snapshot("BTCUSDT")
        before_eth=d.scorer.snapshot("ETHUSDT")
        assert d.status()["symbols"]=={"BTCUSDT":2,"ETHUSDT":2}
    with DurableS3ForwardResearch(path) as d:
        assert d.scorer.snapshot("BTCUSDT")==before_btc
        assert d.scorer.snapshot("ETHUSDT")==before_eth


def test_stale_and_future_receipt_are_rejected_without_append(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path) as d:
        send(d,0)
        bar=candle(1)
        altered=shadow(bar)
        earlier=bar.closed_at+timedelta(seconds=1)
        altered["as_of_utc"]=earlier.isoformat()
        with pytest.raises(S3JournalBlocked,match="unsafe"):
            d.record(bar,altered,observed_at=earlier)
        assert d.status()["observations"]==1


def test_directory_and_symlink_protection(tmp_path):
    with pytest.raises(S3JournalBlocked,match="directory"):
        DurableS3ForwardResearch(tmp_path/"absent"/"events.db")
    p=tmp_path/"original.db"
    with DurableS3ForwardResearch(p):
        pass
    alias=tmp_path/"alias.db"
    alias.symlink_to(p)
    with pytest.raises(S3JournalBlocked,match="non-symlink"):
        DurableS3ForwardResearch(alias)


def test_recovery_refuses_incompatible_schema(tmp_path):
    path=tmp_path/"events.db"
    with DurableS3ForwardResearch(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("UPDATE s3_schema SET version='BAD_VERSION'")
        db.commit()
    with pytest.raises(S3JournalBlocked,match="Incompatible"):
        DurableS3ForwardResearch(path)
