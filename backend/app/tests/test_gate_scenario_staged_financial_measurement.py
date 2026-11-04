import hashlib
import json
import sqlite3
import zipfile

import pytest

from scripts import gate_scenario_staged_financial_measurement as driver


def fixture_source(tmp_path,monkeypatch):
    payload=b'{"synthetic_fixture":true}'
    source={'prior_source_intent_sha256':hashlib.sha256(payload).hexdigest(),
        'supplement_intent_sha256':hashlib.sha256(payload).hexdigest(),
        'prior_raw_hashes':{},'supplement_raw_hashes':{}}
    monkeypatch.setattr(driver.f,'load_sources',lambda old,root:({'fixed':1},source))
    monkeypatch.setattr(driver.f,'Context',lambda data:None)
    monkeypatch.setattr(driver.f,'replay',lambda data,context,block,scenario:{
        'block':block+1,'scenario':scenario,'closed_trades':1,
        'net_closed_point':-1.,'net_closed_lower':-1.1,'screen_passed':False})
    for name in ('prior-source/source-intent.json','source-supplement-intent.json'):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload)
    return driver.register(tmp_path,'unused')


def checkpoint(root,registration):
    archive=root.parent/(root.name+'-fixture.zip')
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('registered-study.json',json.dumps(registration))
        z.write(driver.database(root),'study/staged-financial-measurement.sqlite3')
        for n in ('prior-source/source-intent.json','source-supplement-intent.json'):z.write(root/n,n)
    receipt={'archive_name':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
             'library_file_id':'libfile_synthetic_test_only'}
    (root/'durable-input-receipt.json').write_text(json.dumps(receipt))
    return archive


def test_no_financial_run_before_input_bundle_is_preserved(tmp_path,monkeypatch):
    fixture_source(tmp_path,monkeypatch)
    with pytest.raises(FileNotFoundError):driver.run(tmp_path,'unused')
    assert driver.review(tmp_path)['phases']==['REGISTERED']


def test_checkpoint_matches_registration_and_records_exactly_six_cases_without_retry(tmp_path,monkeypatch):
    registration=fixture_source(tmp_path,monkeypatch)
    checkpoint(tmp_path,registration)
    result=driver.run(tmp_path,'unused')
    assert result['phases']==['REGISTERED','STARTED','RESULT']
    assert result['result']['new_financial_case_replays']==6
    assert not result['result']['profitability_proven']
    assert not result['result']['execution_authorized']
    with driver.connect(tmp_path,True) as db:
        values,_=driver.events(db)
    assert values[1][1]['durable_input_checkpoint']['library_file_id']=='libfile_synthetic_test_only'
    with pytest.raises(ValueError,match='already started'):driver.run(tmp_path,'unused')
    with pytest.raises(FileExistsError):driver.register(tmp_path,'unused')


def test_modified_archive_is_rejected_before_started(tmp_path,monkeypatch):
    registration=fixture_source(tmp_path,monkeypatch)
    archive=checkpoint(tmp_path,registration)
    with archive.open('ab') as out:out.write(b'changed')
    with pytest.raises(ValueError,match='archive changed'):driver.run(tmp_path,'unused')
    assert driver.review(tmp_path)['phases']==['REGISTERED']


def test_archive_cannot_claim_another_registration_even_with_matching_zip_digest(tmp_path,monkeypatch):
    registration=fixture_source(tmp_path,monkeypatch)
    changed=json.loads(json.dumps(registration));changed['registration']['data_sha256']='changed'
    checkpoint(tmp_path,changed)
    with pytest.raises(ValueError,match='registration mismatch'):driver.run(tmp_path,'unused')
    assert driver.review(tmp_path)['phases']==['REGISTERED']


def test_changed_data_after_checkpoint_is_failed_and_not_retried(tmp_path,monkeypatch):
    registration=fixture_source(tmp_path,monkeypatch);checkpoint(tmp_path,registration)
    monkeypatch.setattr(driver.f,'load_sources',lambda old,root:({'changed':1},{}))
    with pytest.raises(ValueError,match='data changed'):driver.run(tmp_path,'unused')
    result=driver.review(tmp_path)
    assert result['phases']==['REGISTERED','STARTED','FAILED'] and result['result'] is None
    with pytest.raises(ValueError,match='already started'):driver.run(tmp_path,'unused')


def test_append_only_chain_and_code_freeze(tmp_path,monkeypatch):
    fixture_source(tmp_path,monkeypatch)
    with sqlite3.connect(driver.database(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):db.execute('DELETE FROM events')
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):db.execute("UPDATE events SET payload='{}'")
    monkeypatch.setattr(driver,'fingerprint',lambda:'changed')
    with pytest.raises(ValueError,match='Frozen'):driver.review(tmp_path)
