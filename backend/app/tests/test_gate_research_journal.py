import copy
import os
import tempfile
import unittest
from app.demo.gate_research_journal import ResearchJournal, InvalidResearch


def sample():
    return {'exchange':'GATE', 'market':'GATE_USDT_FUTURES_TESTNET',
            'analysis_scope':'RESEARCH_ONLY', 'execution_enabled':False, 'source':'AGENTS',
            'symbol':'BTCUSDT', 'timeframe':'15m', 'candle_closed_at':'2026-10-29T13:45:00Z',
            'reference_price':80000, 'reference_price_type':'LAST_CLOSED_CANDLE',
            'decision':{'candidate_action':'WAIT'}, 'total_agents':2,
            'agents':[{'agent_name':'TrendAgent','status':'OK','signal':'WAIT','confidence':50, 'evidence':{'secret':'not stored'}},
                      {'agent_name':'QuantAgent','status':'SKIPPED','signal':'WAIT','confidence':0}]}


class ResearchJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'research.sqlite')
        self.j = ResearchJournal(self.path)

    def tearDown(self):
        self.j.close()
        self.temp.cleanup()

    def test_persistence_dedup_conflict_and_revision(self):
        data = sample()
        self.assertTrue(self.j.record(data, 'abcdef1'))
        self.assertFalse(self.j.record(data, 'abcdef1'))
        changed = copy.deepcopy(data)
        changed['agents'][0]['signal'] = 'BUY'
        with self.assertRaises(InvalidResearch):
            self.j.record(changed, 'abcdef1')
        self.assertTrue(self.j.record(changed, 'abcdef2'))
        self.j.close()
        self.j = ResearchJournal(self.path)
        rows = self.j.db.execute('SELECT agents FROM research').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotIn('secret', str(rows))

    def test_rejects_unsafe_and_malformed_evidence(self):
        changes = [('execution_enabled', True), ('source', 'OPERATIONAL_CANARY'),
                   ('market', 'LIVE'), ('reference_price', float('nan')),
                   ('total_agents', 3), ('candle_closed_at', '2026-10-29T13:45:00')]
        for field, value in changes:
            data = sample()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(InvalidResearch):
                self.j.record(data, 'abcdef1')
        duplicate = sample()
        duplicate['agents'][1]['agent_name'] = 'TrendAgent'
        with self.assertRaises(InvalidResearch):
            self.j.record(duplicate, 'abcdef1')
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM research').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
