"""Research-only walk-forward replay for an agent's actual immutable predicates.

Unlike the D1 baseline bridge, this evaluates the agent candidate itself.
No optimizer or hypothesis rewriting uses the validation or test segment.
This is a preliminary statistical screen, not signed OOS proof or promotion.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from typing import Sequence

from app.backtesting.acceptance import _one_sided_sign_test_p_value
from app.backtesting.walk_forward import plan_walk_forward_windows
from app.schemas.backtest import WalkForwardProtocol, WalkForwardResearchPlan
from app.schemas.market import Candle
from app.strategy.candidate_replay import CandidateReplayReport, replay_candidate
from app.strategy.research_lab import StrategyHypothesis
from app.strategy.research_memory import ResearchMemory


@dataclass(frozen=True)
class CandidateFold:
    index: int
    validation_hash: str
    test_hash: str
    validation_trades: int
    test_trades: int
    validation_net_return_percent: float
    test_net_return_percent: float
    test_max_drawdown_percent: float


@dataclass(frozen=True)
class CandidateWalkForwardResult:
    fingerprint: str
    dataset_hash: str
    trial_index: int
    trial_budget: int
    folds: tuple[CandidateFold, ...]
    test_trades: int
    profitable_test_folds: int
    median_test_net_return_percent: float
    worst_test_drawdown_percent: float
    adjusted_sign_test_p: float
    candidate_artifact_hash: str
    status: str = "RESEARCH_ONLY"
    execution_authorized: bool = False


def evaluate_candidate_walk_forward(
    hypothesis: StrategyHypothesis,
    bars: Sequence[Candle],
    *,
    journal: ResearchMemory,
    protocol: WalkForwardProtocol | None = None,
    research_plan: WalkForwardResearchPlan | None = None,
    all_in_cost_bps_per_side: float = 12.0,
) -> CandidateWalkForwardResult:
    protocol = protocol or WalkForwardProtocol()
    plan = research_plan or WalkForwardResearchPlan()
    if protocol.embargo_candles < 1:
        raise ValueError("Positive embargo required")
    if protocol.max_folds < 3:
        raise ValueError("At least three pre-registered folds required")
    if plan.candidate_budget < plan.candidate_index:
        raise ValueError("Invalid candidate budget")
    if hypothesis.symbol != bars[0].symbol or hypothesis.timeframe != bars[0].timeframe:
        raise ValueError("Candidate/data universe mismatch")
    ordered, windows = plan_walk_forward_windows(list(bars), protocol)
    if len(windows) < 3:
        raise ValueError("Insufficient historical data for three folds")
    folds: list[CandidateFold] = []
    for fold_index, (_, validation, test) in enumerate(windows):
        def evaluate(segment) -> CandidateReplayReport:
            # Warm-up comes ONLY from candles before the evaluated segment;
            # trades cannot be initiated during the warm-up.
            left = max(0, segment.start_index - 60)
            if segment.start_index - left < 60:
                raise ValueError("At least 60 prior candles required for unbiased warm-up")
            return replay_candidate(
                hypothesis, ordered[left:segment.end_index_exclusive],
                all_in_cost_bps_per_side=all_in_cost_bps_per_side,
                evaluation_start=segment.start_index - left,
            )
        validation_report = evaluate(validation)
        test_report = evaluate(test)
        folds.append(CandidateFold(
            index=fold_index,
            validation_hash=validation_report.dataset_hash,
            test_hash=test_report.dataset_hash,
            validation_trades=len(validation_report.trades),
            test_trades=len(test_report.trades),
            validation_net_return_percent=validation_report.net_return_percent,
            test_net_return_percent=test_report.net_return_percent,
            test_max_drawdown_percent=test_report.max_drawdown_percent,
        ))
    pnl = [fold.test_net_return_percent for fold in folds]
    positive = sum(value > 0 for value in pnl)
    count = sum(fold.test_trades for fold in folds)
    adjusted_p = min(
        1.0, _one_sided_sign_test_p_value(positive, len(folds))
        * plan.candidate_budget,
    )
    digest_payload = {
        "hypothesis": hypothesis.fingerprint,
        "folds": [asdict(fold) for fold in folds],
        "cost_bps_side": all_in_cost_bps_per_side,
        "protocol": protocol.model_dump(mode="json"),
        "research_plan": plan.model_dump(mode="json"),
    }
    candidate_hash = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, allow_nan=False,
                   separators=(",", ":")).encode()
    ).hexdigest()
    result = CandidateWalkForwardResult(
        fingerprint=hypothesis.fingerprint,
        dataset_hash=hashlib.sha256(
            "\n".join(f.test_hash for f in folds).encode()
        ).hexdigest(),
        trial_index=plan.candidate_index,
        trial_budget=plan.candidate_budget,
        folds=tuple(folds),
        test_trades=count,
        profitable_test_folds=positive,
        median_test_net_return_percent=statistics.median(pnl),
        worst_test_drawdown_percent=max(f.test_max_drawdown_percent for f in folds),
        adjusted_sign_test_p=adjusted_p,
        candidate_artifact_hash=candidate_hash,
    )
    # Independent immutable result attestations + an actual sealed holdout
    # do NOT exist yet. Store only research observations.
    journal.register(hypothesis)
    journal.append(
        kind="CANDIDATE_WALK_FORWARD_OBSERVED",
        hypothesis_hash=hypothesis.fingerprint,
        event_id="candidate:" + candidate_hash,
        payload={
            "candidate_hash": candidate_hash,
            "test_trades": count,
            "profitable_test_folds": positive,
            "adjusted_sign_test_p": adjusted_p,
            "worst_test_drawdown_percent": result.worst_test_drawdown_percent,
            "median_test_net_return_percent": result.median_test_net_return_percent,
            "status": "RESEARCH_ONLY",
            "execution_authorized": False,
        },
    )
    journal.verify_chain()
    return result
