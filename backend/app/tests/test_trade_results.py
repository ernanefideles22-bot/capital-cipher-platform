import pytest
from app.demo.trade_results import calculate_closed_trade, ResultNotReconciled


def example(**overrides):
    data = dict(side='LONG', entry_fills=[{'quantity':2,'price':100},{'quantity':1,'price':110}],
                exit_fills=[{'quantity':3,'price':120}], contract_multiplier='0.01',
                entry_fees_usdt='0.1', exit_fees_usdt='0.2', funding_net_usdt='-0.05')
    data.update(overrides)
    return calculate_closed_trade(**data)


def test_long_partial_fills_costs_and_funding():
    assert example()['net_pnl_usdt'] == '0.15'
    assert example()['result'] == 'WIN'


def test_short_and_realized_costs():
    result=example(side='SHORT', exit_fills=[{'quantity':3,'price':90}], funding_net_usdt='0.05')
    assert result['net_pnl_usdt'] == '0.15'


@pytest.mark.parametrize('override', [
    {'exit_fills':[{'quantity':2,'price':120}]}, {'entry_fees_usdt':None},
    {'exit_fees_usdt':'-1'}, {'funding_net_usdt':'NaN'}, {'contract_multiplier':0},
    {'side':'BUY'}, {'entry_fills':[]}, {'exit_fills':[{'quantity':True,'price':120}]},
])
def test_unreconciled_result_is_rejected(override):
    with pytest.raises(ResultNotReconciled):
        example(**override)


def test_flat_is_not_a_win():
    result=example(exit_fills=[{'quantity':3,'price':110}], entry_fees_usdt='0.1',
                   exit_fees_usdt='0.1', funding_net_usdt='0')
    assert result['result']=='FLAT'
