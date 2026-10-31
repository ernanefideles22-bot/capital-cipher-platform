"""D2.2: historical data provenance, netted strategy scheduling and pairing."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.candidate_replay import CandidateReplayReport, CandidateTrade
from app.strategy.candidate_venue_scheduler import (
    StrategyTargetEvent, candidate_trades_to_targets,
    compile_oneway_targets, compare_candidate_to_baseline,
    run_candidate_portfolio,
)
from app.strategy.venue_research_catalog import (
    build_tape_dataset, load_tape_dataset, save_tape_dataset,
)
from app.strategy.venue_research_execution import (
    ContractSpec, FundingEvent, Quote,
)

START = datetime(2026, 2, 1, tzinfo=timezone.utc)
SPEC = ContractSpec("BTC_USDT", multiplier=.001, tick_size=.1, maximum_contracts=100)


def tm(sec):
    return START + timedelta(seconds=sec)


def quote(sec, *, observed=None, received=None):
    return Quote(
        quote_id=f"gate-source-{sec}",
        contract="BTC_USDT",
        observed_at=tm(sec if observed is None else observed),
        received_at=tm(sec if received is None else received),
        bid=10000 + sec / 100,
        ask=10001 + sec / 100,
        bid_depth_contracts=20,
        ask_depth_contracts=20,
        taker_fee_bps=5,
    )


def archive(*, funding=()):
    return build_tape_dataset(
        SPEC, [quote(i) for i in (0, 10, 20, 30, 40, 50, 60)],
        funding,
        source="Gate public futures archived test fixture",
        source_archive_id="public-archive-test-set-001",
        collected_at=tm(100),
    )


def candidate_report(fingerprint: str, entry=10, exit=30, *,
                     side="BUY", quantity=.003):
    trade = CandidateTrade(
        entry_at=tm(entry), exit_at=tm(exit), side=side,
        entry_price=10000, exit_price=10000, quantity=quantity,
        stop_price=9900, target_price=10200, exit_reason="REPLAY_END",
        gross_pnl=0, execution_costs=1, net_pnl=-1,
        entry_signal_received_at=tm(entry-1),
        exit_signal_received_at=tm(exit),
    )
    return CandidateReplayReport(
        fingerprint=fingerprint, dataset_hash="f"*64,
        symbol="BTCUSDT", timeframe="15m",
        bars_processed=100, trades=(trade,), net_pnl=-1,
        net_return_percent=-0.01, total_costs=1,
        max_drawdown_percent=.01, skipped_stale_entries=0,
        all_in_cost_bps_per_side=12,
    )


def event(event_id, strategy, action, t, target):
    return StrategyTargetEvent(
        event_id=event_id, strategy_key=strategy,
        event_type=action, received_at=tm(t-1),
        execution_at=tm(t), target_signed_contracts=target,
    )


def test_archive_roundtrip_and_replay_referenced_sha(tmp_path):
    data = archive()
    path = tmp_path / "gate-quotes.json"
    save_tape_dataset(data, path)
    loaded = load_tape_dataset(path)
    assert loaded.manifest == data.manifest
    assert loaded.manifest.quote_count == 7
    assert len(loaded.manifest.dataset_sha256) == 64
    assert loaded.tape().at(tm(30)).quote_id == "gate-source-30"
    with pytest.raises(FileExistsError):
        save_tape_dataset(data, path)
    text = path.read_text()
    path.write_text(text.replace("public-archive-test-set-001", "public-archive-test-set-002"))
    with pytest.raises(ValueError, match="manifest mismatch"):
        load_tape_dataset(path)


def test_future_quote_capture_and_incomplete_sources_rejected():
    with pytest.raises(ValueError, match="Future quote"):
        build_tape_dataset(
            SPEC, [quote(0), quote(200)], source="Gate fixture",
            source_archive_id="archived-quote-1234", collected_at=tm(100),
        )
    with pytest.raises(ValueError, match="At least two"):
        build_tape_dataset(
            SPEC, [quote(0)], source="Gate fixture",
            source_archive_id="archived-quote-1234", collected_at=tm(100),
        )


def test_shared_same_side_positions_and_cross_strategy_netting():
    events = [
        event("a1", "SCALP", "ENTRY", 10, +3),
        event("b1", "DAY", "ENTRY", 20, +2),
        event("a2", "SCALP", "EXIT", 30, 0),
        event("b2", "DAY", "EXIT", 40, 0),
    ]
    plan = compile_oneway_targets(
        events, contract="BTC_USDT", max_contracts=100,
    )
    assert [(x.side, x.contracts, x.reduce_only) for x in plan.intents] == [
        ("BUY", 3, False), ("BUY", 2, False),
        ("SELL", 3, True), ("SELL", 2, True),
    ]
    assert not plan.execution_authorized


def test_long_short_overlap_nets_without_creating_two_venue_books():
    events = [
        event("a1", "SCALP", "ENTRY", 10, +3),
        event("b1", "DAY", "ENTRY", 20, -2),
        event("b2", "DAY", "EXIT", 30, 0),
        event("a2", "SCALP", "EXIT", 40, 0),
    ]
    with pytest.raises(ValueError, match="increase net exposure"):
        compile_oneway_targets(
            events, contract="BTC_USDT", max_contracts=100
        )
    opted_in = compile_oneway_targets(
        events, contract="BTC_USDT", max_contracts=100,
        allow_unhedge_on_exit=True,
    )
    assert opted_in.unhedge_events == 1
    assert [(x.side,x.contracts,x.reduce_only) for x in opted_in.intents] == [
        ("BUY",3,False),("SELL",2,True),
        ("BUY",2,False),("SELL",3,True),
    ]


def test_zero_delta_hedge_rebalance_cannot_hide_risk_on_exit():
    events = [
        event("a1","SCALP","ENTRY",10,+2),
        event("b1","DAY","ENTRY",20,-2),
        event("b2","DAY","EXIT",30,0),
        event("a2","SCALP","EXIT",40,0),
    ]
    with pytest.raises(ValueError,match="increase net exposure"):
        compile_oneway_targets(events,contract="BTC_USDT",max_contracts=100)
    p=compile_oneway_targets(events,contract="BTC_USDT",max_contracts=100,
                             allow_unhedge_on_exit=True)
    assert p.zero_delta_events == 0  # hedging requires a real venue order


def test_reversal_and_duplicate_source_fail_closed():
    events=[
        event("a1","SCALP","ENTRY",10,+1),
        event("b1","DAY","ENTRY",20,-3),
        event("a2","SCALP","EXIT",30,0),
        event("b2","DAY","EXIT",40,0),
    ]
    with pytest.raises(ValueError,match="reversal"):
        compile_oneway_targets(events,contract="BTC_USDT",max_contracts=100)
    with pytest.raises(ValueError,match="Duplicate"):
        compile_oneway_targets([events[0],events[0]],contract="BTC_USDT",max_contracts=100)


def test_candidate_trade_requires_causal_availability():
    r=candidate_report("a"*64)
    intents=candidate_trades_to_targets(
        [r],contract_multiplier=SPEC.multiplier,expected_symbol="BTCUSDT",
    )
    assert len(intents)==2
    assert intents[0].received_at < intents[0].execution_at
    bad=replace(r, trades=(replace(r.trades[0],entry_signal_received_at=None),))
    with pytest.raises(ValueError,match="Missing causal"):
        candidate_trades_to_targets(
            [bad],contract_multiplier=SPEC.multiplier,expected_symbol="BTCUSDT",
        )


def test_candidate_research_portfolio_and_paired_comparison():
    data=archive()
    baseline=candidate_report("a"*64, entry=10,exit=30,side="BUY")
    new=candidate_report("b"*64, entry=20,exit=40,side="SELL")
    plan,venue=run_candidate_portfolio(data,[baseline])
    assert len(plan.intents)==2
    assert venue.final_state.handled_orders==2
    assert venue.final_state.signed_contracts==0
    result=compare_candidate_to_baseline(data,[baseline],[new])
    assert result.archive_sha256==data.manifest.dataset_sha256
    assert result.baseline_fills==2
    assert result.candidate_fills==2
    assert result.incremental_net==pytest.approx(
        result.candidate_portfolio_net-result.baseline_portfolio_net
    )
    assert not result.execution_authorized


def test_replaying_same_candidate_overlap_produces_same_netted_portfolio():
    data=archive()
    first=candidate_report("a"*64,entry=10,exit=40,side="BUY",quantity=.003)
    second=candidate_report("b"*64,entry=20,exit=30,side="BUY",quantity=.002)
    a,b=run_candidate_portfolio(data,[first,second])
    assert [(x.side,x.contracts) for x in a.intents] == [
        ("BUY",3),("BUY",2),("SELL",2),("SELL",3)
    ]
    assert len(b.fills)==4
    assert b.final_state.signed_contracts==0


def test_funding_cost_applied_in_both_paired_runs():
    funding=FundingEvent(
        funding_id="real-archive-fund-1",contract="BTC_USDT",
        settled_at=tm(20),received_at=tm(20),
        mark_price=10000,rate=.001,
    )
    data=archive(funding=[funding])
    baseline=candidate_report("a"*64,entry=10,exit=40,side="BUY")
    _,run=run_candidate_portfolio(data,[baseline])
    assert run.final_state.funding_cost==pytest.approx(.03)
    assert run.final_state.realized_net<0


def test_quote_gap_blocks_synthetic_optimistic_execution():
    base=archive()
    report=candidate_report("a"*64,entry=500,exit=510)
    with pytest.raises(ValueError,match="stale"):
        run_candidate_portfolio(base,[report])
