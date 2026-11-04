import sqlite3

import pytest

from scripts import gate_scenario_financial_measurement as driver


def fake_source(monkeypatch):
    monkeypatch.setattr(driver.f,'load_sources',lambda old,root:({'fixed':1},{'hash':'fixed'}))
    monkeypatch.setattr(driver.f,'Context',lambda data:None)
    monkeypatch.setattr(driver.f,'replay',lambda data,context,block,scenario:{
        'block':block+1,'scenario':scenario,'closed_trades':1,
        'net_closed_point':-1.,'net_closed_lower':-1.1,'screen_passed':False})


def test_measurement_registers_before_run_and_cannot_be_restarted(tmp_path,monkeypatch):
    fake_source(monkeypatch)
    before=driver.register(tmp_path,'unused')
    assert before['phases']==['REGISTERED']
    result=driver.run(tmp_path,'unused')
    assert result['phases']==['REGISTERED','STARTED','RESULT']
    assert result['result']['new_financial_case_replays']==6
    assert not result['result']['profitability_proven']
    assert not result['result']['execution_authorized']
    assert result==driver.review(tmp_path)
    with pytest.raises(ValueError,match='already started'):driver.run(tmp_path,'unused')
    with pytest.raises(FileExistsError):driver.register(tmp_path,'unused')


def test_changed_registered_data_fails_without_producing_a_financial_result(tmp_path,monkeypatch):
    fake_source(monkeypatch);driver.register(tmp_path,'unused')
    monkeypatch.setattr(driver.f,'load_sources',lambda old,root:({'changed':1},{'hash':'fixed'}))
    with pytest.raises(ValueError,match='data changed'):driver.run(tmp_path,'unused')
    review=driver.review(tmp_path)
    assert review['phases']==['REGISTERED','STARTED','FAILED']
    assert review['result'] is None
    with pytest.raises(ValueError,match='already started'):driver.run(tmp_path,'unused')


def test_sqlite_append_only_and_chain_integrity_are_enforced(tmp_path,monkeypatch):
    fake_source(monkeypatch);driver.register(tmp_path,'unused')
    with sqlite3.connect(driver.database(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):db.execute('DELETE FROM events')
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):db.execute("UPDATE events SET payload='{}'")
        db.execute('DROP TRIGGER no_update');db.execute("UPDATE events SET payload='{}'")
    with pytest.raises(ValueError,match='chain mismatch'):driver.review(tmp_path)


def test_code_fingerprint_change_is_not_a_new_valid_trial(tmp_path,monkeypatch):
    fake_source(monkeypatch);driver.register(tmp_path,'unused')
    monkeypatch.setattr(driver,'fingerprint',lambda:'changed')
    with pytest.raises(ValueError,match='Frozen'):driver.review(tmp_path)
