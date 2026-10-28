"""Refuse unsafe hosted targets before any isolated database is created."""
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
hosted = importlib.import_module("validate_railway_phase_c")


@pytest.mark.parametrize("name", ["capital_cipher_staging", "postgres",
    "capital_cipher_final_b_" + "a" * 12 + "_flow",
    "capital_cipher_phase_c_" + "a" * 12 + "_flow;drop database postgres"])
def test_hosted_scope_refuses_main_prior_evidence_and_injection(name):
    with pytest.raises(RuntimeError, match="PHASE_C_ISOLATED_DATABASE_NAME_REFUSED"):
        hosted.isolated_url("postgresql://unused@localhost/capital_cipher_staging", name)


def test_all_generated_names_are_distinct_and_fit_postgresql():
    names = [hosted.PREFIX + "a" * 12 + "_" + kind for kind in hosted.KINDS]
    assert len(names) == len(set(names)) == 11
    for name in names:
        assert len(name.encode()) <= 63
        assert hosted.isolated_url("postgresql://unused@localhost/postgres", name).endswith("/"+name)


def test_explicit_opt_in_required(monkeypatch):
    monkeypatch.delenv("PHASE_C_ISOLATED_VALIDATION_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="PHASE_C_EXPLICIT_OPT_IN_REQUIRED"):
        hosted.require_scope()


def test_wrong_region_refused_before_connecting(monkeypatch):
    monkeypatch.setenv("PHASE_C_ISOLATED_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("APP_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("PAPER_VALIDATION_EXPECTED_REGION", "europe-west4-drams3a")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", "sfo")
    with pytest.raises(RuntimeError, match="PAPER_VALIDATION_REGION_MISMATCH"):
        hosted.require_scope()


async def test_storage_ceiling_refuses_growth_without_deleting_evidence():
    class Large:
        async def fetchval(self, query, name):
            return hosted.MAX_TOTAL_DATABASE_BYTES + 1
    with pytest.raises(RuntimeError, match="PHASE_C_STORAGE_BUDGET_EXCEEDED"):
        await hosted.retained_sizes(Large(), {"flow": "retained"})
