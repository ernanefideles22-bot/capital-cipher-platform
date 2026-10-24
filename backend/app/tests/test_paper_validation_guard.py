"""The native deployment gate must refuse original or arbitrary databases."""
from pathlib import Path
import importlib

import pytest


def gate_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("validate_railway_paper_flow")


@pytest.mark.parametrize("name", ["capital_cipher_staging", "postgres", "capital_cipher_infra_smoke_20261023"])
def test_gate_refuses_runtime_and_donor_as_write_targets(monkeypatch, name):
    gate = gate_module(monkeypatch)
    with pytest.raises(RuntimeError, match="DATABASE_NAME_REFUSED"):
        gate.database_url("postgresql://localhost/postgres", name)


async def test_gate_requires_explicit_opt_in_before_any_connection(monkeypatch):
    gate = gate_module(monkeypatch)
    monkeypatch.delenv("PAPER_FULL_FLOW_VALIDATION_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="OPT_IN_REQUIRED"):
        await gate.main()
