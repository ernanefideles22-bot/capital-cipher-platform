import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.strategy import gate_scenario_staged_journal as journal
from app.strategy import gate_scenario_staged_levels as observer
from scripts.gate_scenario_demo import synthetic_snapshots


def test_v2_session_never_rewrites_or_replaces_existing_v1_journal(tmp_path):
    from app.strategy import gate_scenario_journal as original
    snapshots=synthetic_snapshots()
    first,second=next(snapshots),next(snapshots)
    prior=original.register(tmp_path,first)
    before=original._path(tmp_path).read_bytes()
    journal.register(tmp_path,first)
    journal.observe(tmp_path,second)
    assert journal._path(tmp_path) != original._path(tmp_path)
    assert original._path(tmp_path).read_bytes() == before
    assert original.review(tmp_path) == prior


@pytest.mark.parametrize("side",["SHORT","LONG"])
@pytest.mark.parametrize("case,expected",[("confirmed","CANDIDATE_ONLY"),
    ("failed_recheck","INVALIDATED"),("invalidated","INVALIDATED")])
def test_real_s1_full_minute_sequence_persists_and_restarts_without_orders(tmp_path,side,case,expected):
    snapshots = synthetic_snapshots(case,side=side)
    first = next(snapshots)
    result = journal.register(tmp_path,first)
    assert result["status"] == "WAIT_TOUCH"
    before = hashlib.sha256(journal._path(tmp_path).read_bytes()).hexdigest()
    assert journal.observe(tmp_path,first)["events"] == 1
    assert hashlib.sha256(journal._path(tmp_path).read_bytes()).hexdigest() == before
    for payload in snapshots:
        result = journal.observe(tmp_path,payload)
        assert result["status"] != "BLOCKED", result["last_error"]
        if result["status"] in observer.TERMINAL:
            break
    assert result["status"] == expected
    assert result["new_candidate_count"] == (1 if expected == "CANDIDATE_ONLY" else 0)
    assert not result["execution_authorized"] and not result["deployed_service_connected"]
    assert result == journal.review(tmp_path)
    if expected == "CANDIDATE_ONLY":
        stages = list(dict.fromkeys(t["stage"] for t in result["transitions"]))
        assert stages == ["WAIT_TOUCH","WAIT_REJECTION","WAIT_BREAK","WAIT_RECHECK","CANDIDATE_ONLY"]
        retry = journal.observe(tmp_path,payload)
        assert retry == result
        candidate = result["state"]["candidate"]
        assert candidate["proposal_id"]
        assert not candidate["profitability_proven"]


def test_history_revision_is_recorded_as_failure_and_session_stays_blocked(tmp_path):
    snapshots = synthetic_snapshots()
    first, second = next(snapshots), next(snapshots)
    journal.register(tmp_path,first)
    changed = copy.deepcopy(second)
    changed["candles"]["4h"][0]["volume"] += 1
    value = journal.observe(tmp_path,changed)
    assert value["status"] == "BLOCKED"
    assert "history changed" in value["last_error"]
    with pytest.raises(ValueError,match="Session blocked"):
        journal.observe(tmp_path,second)
    with pytest.raises(FileExistsError):
        journal.register(tmp_path,first)


def test_missing_minute_blocks_instead_of_ignoring_unseen_price_path(tmp_path):
    snapshots = iter(synthetic_snapshots())
    journal.register(tmp_path,next(snapshots))
    next(snapshots)
    value = journal.observe(tmp_path,next(snapshots))
    assert value["status"] == "BLOCKED"
    assert "Minute observation gap" in value["last_error"]


def test_concurrent_retry_appends_exactly_one_observation(tmp_path):
    snapshots=synthetic_snapshots()
    journal.register(tmp_path,next(snapshots))
    payload=next(snapshots)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reports=list(workers.map(lambda _:journal.observe(tmp_path,payload),range(2)))
    assert all(r["events"] == 2 for r in reports)
    assert journal.review(tmp_path)["events"] == 2
    assert journal.review(tmp_path)["new_candidate_count"] == 0


def test_future_data_and_stale_snapshot_cannot_create_a_session(tmp_path):
    payload = next(synthetic_snapshots())
    payload["candles"]["15m"][-1]["received_at"] = "2026-11-03T12:01:00Z"
    with pytest.raises(ValueError,match="Future"):
        journal.register(tmp_path,payload)
    assert not journal._path(tmp_path).exists()


def test_append_only_and_hash_chain_detect_storage_corruption(tmp_path):
    journal.register(tmp_path,next(synthetic_snapshots()))
    with sqlite3.connect(journal._path(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):
            db.execute("UPDATE events SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):
            db.execute("DELETE FROM events")
        db.execute("DROP TRIGGER no_update")
        db.execute("UPDATE events SET payload='{}'")
    with pytest.raises(ValueError,match="chain mismatch"):
        journal.review(tmp_path)


def test_changed_code_fingerprint_requires_review_and_does_not_reset_journal(tmp_path,monkeypatch):
    journal.register(tmp_path,next(synthetic_snapshots()))
    monkeypatch.setattr(journal,"fingerprint",lambda:"changed")
    with pytest.raises(ValueError,match="source, phase"):
        journal.review(tmp_path)


def test_cli_analyses_markings_and_reads_durable_state_in_a_new_process(tmp_path):
    payload = next(synthetic_snapshots())
    src, output = tmp_path/"snapshot.json",tmp_path/"analysis.json"
    src.write_text(json.dumps(payload))
    command = [sys.executable,"-m","scripts.gate_scenario_staged_levels"]
    subprocess.run([*command,"analyze","--snapshot",str(src),"--output",str(output)],check=True)
    analysis = json.loads(output.read_text())
    assert analysis["plan"]["side"] == "SHORT"
    assert analysis["annotations"]["4h"]["resistance_line_confirmed_anchors"]
    assert "fibonacci_levels" in analysis["annotations"]["15m"]
    root=tmp_path/"session"
    journal.register(root,payload)
    report=tmp_path/"review.json"
    subprocess.run([*command,"review","--root",str(root),"--output",str(report)],check=True)
    assert json.loads(report.read_text()) == journal.review(root)
    before=journal.review(root)["journal_head"]
    completed=subprocess.run([*command,"observe","--root",str(root),"--snapshot",str(src),
                              "--output",str(report)],capture_output=True)
    assert completed.returncode != 0  # Existing output cannot cause hidden mutation.
    assert journal.review(root)["journal_head"] == before


def test_snapshot_budget_and_journal_symlink_are_refused(tmp_path):
    payload=next(synthetic_snapshots())
    payload["extra"]="x"*journal.MAX_SNAPSHOT_BYTES
    with pytest.raises(ValueError,match="byte budget"):
        journal.register(tmp_path,payload)
    linked=tmp_path/"link"
    target=tmp_path/"target"
    target.mkdir()
    linked.symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match="symlink"):
        journal.register(linked,next(synthetic_snapshots()))
