"""Stage S2E: passive S1 -> S2 research bridge on SAME observed Gate candles.

Uses only public OHLCV already fetched by Stage 1. No fresh network request,
secret, account API, agent registry, OMS, risk-manager operation, database,
order object, broker action, state mutation, promotion or position creation.
Returns non-executable hypotheses for inspection in SHADOW.
"""
from __future__ import annotations

from datetime import datetime,timezone
from typing import Mapping,Sequence

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES,evaluate_strategy
from app.strategy.gate_s2c_confluence import evaluate_confluence
from app.strategy.professional_gate_chart_stage1 import (
    MultiTimeframeReading, analyze_gate_multi_timeframe,
)

S2E_POLICY="S2C_MTF_TREND_RANGE_V1_LOCKED_20261101"
S2E_POLICY_COMMIT="8813431f2d80408241e9befe2a25ab4c5c0245a5"
S2E_RESEARCH_FRAME="15m"


def evaluate_shared_shadow(
    symbol: str, by_timeframe: Mapping[str,Sequence[Candle]], *,
    as_of: datetime,
) -> dict:
    """Score three frozen S2 hypotheses against a single trusted S1 context.

    Uses ACTUAL candle receipt clocks; no historical replay or synthesized
    observation stamps. Return only read-only, non-executable summaries.
    """
    if symbol not in {"BTCUSDT","ETHUSDT","SOLUSDT"}:
        raise ValueError("Unapproved Gate S2E market")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Naive live research cutoff")
    cutoff=as_of.astimezone(timezone.utc)
    if set(by_timeframe)!={"15m","1h","4h"}:
        raise ValueError("Incomplete shared S1 market context")
    # Deep copies prevent secondary analysis from changing the S1 in-memory
    # market observations, even via mutable Pydantic candle objects.
    closed={tf:tuple(bar.model_copy(deep=True) for bar in bars)
            for tf,bars in by_timeframe.items()}
    chart:MultiTimeframeReading=analyze_gate_multi_timeframe(
        closed,as_of=cutoff)
    if (chart.exchange!="GATE" or chart.symbol!=symbol
            or chart.trading_authorized is not False):
        raise ValueError("Unsafe S1 professional chart context")
    evidence=[]
    allowed=rejected=0
    for strategy_id in sorted(STRATEGIES):
        proposal=evaluate_strategy(
            strategy_id,closed[S2E_RESEARCH_FRAME],
            as_of=cutoff,historical_replay=False)
        if (proposal.execution_authorized is not False
                or proposal.predictive_edge_proven is not False):
            raise ValueError("Executable strategy proposal rejected")
        verdict=evaluate_confluence(proposal,chart)
        if verdict.authority_to_trade is not False:
            raise ValueError("Confluence attempted unauthorized trade")
        if verdict.original_action=="WAIT":
            disposition="NO_PROPOSAL"
        elif verdict.filtered_action=="WAIT":
            disposition="FILTERED"
            rejected+=1
        else:
            disposition="HYPOTHESIS_ONLY"
            allowed+=1
        evidence.append({
            "strategy_id":strategy_id,
            "strategy_version":proposal.strategy_version,
            "timeframe":S2E_RESEARCH_FRAME,
            "proposed_action":proposal.action,
            "filtered_action":verdict.filtered_action,
            "disposition":disposition,
            "rejection_reasons":list(verdict.reasons),
            "stop_distance":proposal.stop_distance,
            "target_distance":proposal.target_distance,
            "execution_authorized":False,
            "predictive_edge_proven":False,
        })
    return {
        "stage":"PRO_TRADER_S2E",
        "status":"READ_ONLY_CONFLUENCE_READY",
        "mode":"PASSIVE_S1_SHARED_CANDLES_SHADOW",
        "policy_id":S2E_POLICY,
        "policy_commit":S2E_POLICY_COMMIT,
        "symbol":symbol,
        "as_of_utc":cutoff.isoformat(),
        "s1_alignment":chart.alignment,
        "s1_frames":[r.timeframe for r in chart.readings],
        "hypotheses":evidence,
        "candidate_count":allowed,
        "filtered_count":rejected,
        "source_decision_cohort_modified":False,
        "oms_touched":False,
        "source_exchange_authenticated":False,
        "predictive_edge_proven":False,
        "execution_authorized":False,
    }
