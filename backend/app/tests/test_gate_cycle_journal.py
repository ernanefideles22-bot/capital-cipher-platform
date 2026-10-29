import pytest
from app.demo.gate_cycle_journal import GateCycleJournal, CycleBlocked


def setup(path, *, size=2):
    j=GateCycleJournal(path)
    j.create('trade_12345','BTC_USDT','79000',size)
    j.record_entry_intent('trade_12345')
    return j


def snapshot(*, size=0, status='open'):
    return {'id_string':'123','status':status,'create_time':1000,'order_type':'close-long-position',
            'initial':{'contract':'BTC_USDT','size':size,'price':'0','tif':'ioc','is_reduce_only':True,'is_close':True},
            'trigger':{'strategy_type':0,'price_type':1,'price':'79000','rule':2,'expiration':3600}}


def confirm_stop(j, row, position=2):
    j.record_stop_intent('trade_12345')
    j.record_stop_acknowledgement('trade_12345',stop_id='123')
    return j.record_stop_confirmation('trade_12345',snapshot=row,observed_at=1010,now=1010,confirmed_position=position)


def test_complete_simulated_cycle_is_durable(tmp_path):
    path=str(tmp_path/'cycle.sqlite')
    j=setup(path)
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    assert confirm_stop(j,snapshot())
    assert j.read('trade_12345')['state']=='PROTECTED'
    j.record_exit_intent('trade_12345')
    assert j.record_flat_confirmation('trade_12345',position=0,open_orders=0,open_stop_orders=0,all_positions_flat=True,exchange_confirmed=True)
    j.close()
    j=GateCycleJournal(path)
    assert j.read('trade_12345')['state']=='CLOSED'
    with pytest.raises(CycleBlocked):
        j.record_entry_intent('trade_12345')
    assert j.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==8
    j.close()


@pytest.mark.parametrize('confirmed,size',[(False,2),(True,0),(True,-2),(True,3)])
def test_bad_entry_evidence_halts(tmp_path,confirmed,size):
    j=setup(str(tmp_path/'cycle.sqlite'))
    j.record_entry_confirmation('trade_12345',signed_position=size,owned_order_confirmed=confirmed)
    assert j.read('trade_12345')['state']=='HALTED'
    with pytest.raises(CycleBlocked):
        j.record_stop_intent('trade_12345')
    j.close()


def test_partial_fill_protection_uses_actual_position(tmp_path):
    j=setup(str(tmp_path/'cycle.sqlite'))
    j.record_entry_confirmation('trade_12345',signed_position=1,owned_order_confirmed=True)
    assert not confirm_stop(j,snapshot(),position=2)
    assert j.read('trade_12345')['state']=='HALTED'
    j.close()


@pytest.mark.parametrize('position',[None, True, 'NaN', 'garbage'])
def test_invalid_position_evidence_halts_and_persists(tmp_path, position):
    path=str(tmp_path/'cycle.sqlite')
    j=setup(path)
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    assert not confirm_stop(j,snapshot(),position=position)
    assert j.read('trade_12345')['state']=='HALTED'
    j.close()
    j=GateCycleJournal(path)
    assert j.read('trade_12345')['state']=='HALTED'
    with pytest.raises(CycleBlocked):
        j.create('other_12345','BTC_USDT','79000',1)
    j.close()


def test_restart_during_ambiguous_submission_never_resubmits(tmp_path):
    path=str(tmp_path/'cycle.sqlite')
    j=setup(path)
    j.close()
    j=GateCycleJournal(path)
    assert j.read('trade_12345')['state']=='ENTRY_PENDING'
    assert j.recovery_status()=={'recovery_required':True,'execution_authorized':False,
                                 'cycles':[{'id':'trade_12345','state':'ENTRY_PENDING','contract':'BTC_USDT'}]}
    with pytest.raises(CycleBlocked):
        j.record_entry_intent('trade_12345')
    j.halt_unknown('trade_12345')
    assert j.read('trade_12345')['state']=='HALTED'
    j.close()


def test_completed_cycle_does_not_require_recovery(tmp_path):
    j=setup(str(tmp_path/'cycle.sqlite'))
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    assert confirm_stop(j,snapshot())
    j.record_exit_intent('trade_12345')
    assert j.record_flat_confirmation('trade_12345',position=0,open_orders=0,open_stop_orders=0,
                                      all_positions_flat=True,exchange_confirmed=True)
    assert j.recovery_status()=={'recovery_required':False,'execution_authorized':False,'cycles':[]}
    j.close()


def test_unresolved_cycle_blocks_other_connections_and_restart(tmp_path):
    path=str(tmp_path/'cycle.sqlite')
    first=setup(path)
    second=GateCycleJournal(path)
    with pytest.raises(CycleBlocked):
        second.create('other_12345','ETH_USDT','3000',1)
    first.halt_unknown('trade_12345')
    first.close();second.close()
    reopened=GateCycleJournal(path)
    with pytest.raises(CycleBlocked):
        reopened.create('other_12345','ETH_USDT','3000',1)
    reopened.close()


def test_stop_submission_lost_ack_remains_blocked_after_restart(tmp_path):
    path=str(tmp_path/'cycle.sqlite')
    j=setup(path)
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    j.record_stop_intent('trade_12345')
    j.close()
    j=GateCycleJournal(path)
    assert j.read('trade_12345')['state']=='STOP_SUBMITTING'
    with pytest.raises(CycleBlocked):
        j.record_stop_intent('trade_12345')
    j.halt_unknown('trade_12345')
    with pytest.raises(CycleBlocked):
        j.record_exit_intent('trade_12345')
    j.close()


@pytest.mark.parametrize('status,position,orders,confirmed', [
    ('finished',0,0,True), ('open',1,0,True), ('open',0,1,True), ('open',0,0,False),
])
def test_unconfirmed_protection_or_close_halts(tmp_path,status,position,orders,confirmed):
    j=setup(str(tmp_path/'cycle.sqlite'))
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    if not confirm_stop(j,snapshot(status=status)):
        assert j.read('trade_12345')['state']=='HALTED'
    else:
        j.record_exit_intent('trade_12345')
        assert not j.record_flat_confirmation('trade_12345',position=position,open_orders=orders,open_stop_orders=0,all_positions_flat=True,exchange_confirmed=confirmed)
        assert j.read('trade_12345')['state']=='HALTED'
    j.close()


def test_stop_left_open_never_counts_as_flat(tmp_path):
    j=setup(str(tmp_path/'cycle.sqlite'))
    j.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    assert confirm_stop(j,snapshot())
    j.record_exit_intent('trade_12345')
    assert not j.record_flat_confirmation('trade_12345',position=0,open_orders=0,open_stop_orders=1,all_positions_flat=True,exchange_confirmed=True)
    assert j.read('trade_12345')['state']=='HALTED'
    j.close()

