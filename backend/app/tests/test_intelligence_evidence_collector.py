"""Opt-in research ingestion: no live runtime wiring."""
import pytest

from app.agents.intelligence.evidence_collector import (
    EvidenceLedger, collect_completed_traces,
)
from app.tests.test_intelligence_shadow_observer import make_trace


class Reader:
    def __init__(self, trace):
        self.trace = trace

    async def load_agent_execution_trace(self, execution_id):
        return self.trace if execution_id == self.trace.job.execution_id else None


@pytest.mark.asyncio
async def test_collection_persists_and_is_idempotent(tmp_path):
    trace = make_trace()
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    reader = Reader(trace)
    assert await collect_completed_traces(reader, ledger, [trace.job.execution_id]) == (1, 0)
    assert await collect_completed_traces(reader, ledger, [trace.job.execution_id]) == (0, 1)
    assert EvidenceLedger(tmp_path / "evidence.sqlite3").count() == 1
    assert await collect_completed_traces(reader, ledger, ["b" * 64]) == (0, 1)


@pytest.mark.asyncio
async def test_reject_duplicate_requests(tmp_path):
    trace = make_trace()
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    with pytest.raises(ValueError, match="Duplicate"):
        await collect_completed_traces(
            Reader(trace), ledger, [trace.job.execution_id] * 2
        )
