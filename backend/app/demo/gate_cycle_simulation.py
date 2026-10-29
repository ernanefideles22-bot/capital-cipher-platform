"""Mock-only coordination of a Gate cycle. Never usable with live credentials."""
from app.demo.gate_protocol_simulator import GateProtocolSimulator


class SimulationBlocked(ValueError):
    pass


async def run_simulated_cycle(*, simulator, journal, cycle_id, plan, read_entry,
                              read_stop, read_exit):
    """Exercise sequencing using mock requests and independently supplied evidence.

    This is not an executor and does not enable writes to a real exchange.
    A halted simulation remains locked for inspection.
    """
    if type(simulator) is not GateProtocolSimulator:
        raise SimulationBlocked('mock simulator required')
    entry, stop = plan['entry'], plan['stop']
    contract, target, quantity = entry['contract'], stop['trigger']['price'], entry['size']
    journal.create(cycle_id, contract, target, quantity)
    journal.record_entry_intent(cycle_id)
    try:
        acknowledgement = await simulator.submit_entry(plan)
        evidence = await read_entry(acknowledgement)
        journal.record_entry_confirmation(cycle_id, signed_position=evidence['position'],
                                          owned_order_confirmed=evidence['owned'] is True)
    except Exception:
        journal.halt_unknown(cycle_id)
        return 'HALTED'
    if journal.read(cycle_id)['state'] != 'STOP_REQUIRED':
        return 'HALTED'
    journal.record_stop_intent(cycle_id)
    try:
        acknowledgement = await simulator.submit_stop(plan)
        journal.record_stop_acknowledgement(cycle_id, stop_id=str(acknowledgement['id']))
        evidence = await read_stop(str(acknowledgement['id']))
        valid = journal.record_stop_confirmation(cycle_id, snapshot=evidence['snapshot'],
            observed_at=evidence['observed_at'], now=evidence['now'], confirmed_position=evidence['position'])
    except Exception:
        if journal.read(cycle_id)['state'] in {'STOP_SUBMITTING','STOP_PENDING'}:
            journal.halt_unknown(cycle_id)
        return 'HALTED'
    if not valid:
        return 'HALTED'
    journal.record_exit_intent(cycle_id)
    try:
        acknowledgement = await simulator.submit_exit(plan)
        evidence = await read_exit(acknowledgement)
        closed = journal.record_flat_confirmation(cycle_id, position=evidence['position'],
            open_orders=evidence['open_orders'], open_stop_orders=evidence['open_stop_orders'],
            all_positions_flat=evidence['all_positions_flat'], exchange_confirmed=evidence['exchange_confirmed'])
    except Exception:
        journal.halt_unknown(cycle_id)
        return 'HALTED'
    return 'CLOSED' if closed else 'HALTED'
