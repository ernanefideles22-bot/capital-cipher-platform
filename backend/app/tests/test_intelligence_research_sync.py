"""Research synchronization must remain opt-in and read-only."""
import pytest

from app.agents.intelligence.evidence_collector import EvidenceLedger
from app.agents.intelligence.research_sync import sync_terminal_paper_executions
from app.tests.test_intelligence_shadow_observer import make_trace


class ReadOnlyRepository:
    def __init__(self, trace):
        self.trace = trace
        self.list_calls = 0
        self.trace_calls = 0

    async def list_agent_execution_jobs(self, *, limit=100):
        self.list_calls += 1
        return [self.trace.job][:limit]

    async def load_agent_execution_trace(self, execution_id):
        self.trace_calls += 1
        return self.trace if execution_id == self.trace.job.execution_id else None


@pytest.mark.asyncio
async def test_sync_only_reads_and_deduplicates(tmp_path):
    repo = ReadOnlyRepository(make_trace())
    ledger = EvidenceLedger(tmp_path / "observations.sqlite3")
    first = await sync_terminal_paper_executions(repo, ledger)
    second = await sync_terminal_paper_executions(repo, ledger)
    assert (first.inspected, first.eligible, first.inserted, first.skipped) == (1, 1, 1, 0)
    assert (second.inserted, second.skipped) == (0, 1)
    assert repo.list_calls == 2
    assert repo.trace_calls == 2
    assert ledger.count() == 1
    assert not first.order_authority
    assert not first.decision_authority


@pytest.mark.asyncio
async def test_reject_invalid_limit(tmp_path):
    repo = ReadOnlyRepository(make_trace())
    with pytest.raises(ValueError):
        await sync_terminal_paper_executions(
            repo, EvidenceLedger(tmp_path / "observations.sqlite3"), limit=0
        )
