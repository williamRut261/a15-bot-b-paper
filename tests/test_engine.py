import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from a15_fill_bot import PaperFillBot

SRC = Path(__file__).resolve().parents[1]/'original_a15.json'

class PaperBotTests(unittest.TestCase):
    def make(self, callback, slippage='0'):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        b=PaperFillBot(str(SRC),str(Path(self.tmp.name)/'test.sqlite'), callback,
                       slippage_bps=slippage)
        b.set_prices({'GDXU':50,'BIL':100},'2026-11-19T20:00:00+00:00')
        return b

    def test_full_recalculation_on_each_fill(self):
        calls=[]
        def a15_eval(ts,state,ast):
            calls.append(tuple(sorted(state.positions)))
            self.assertEqual(ast['name'],'A15-Miner Rider|10/1')
            return {'GDXU': Decimal('.5'), 'BIL': Decimal('.5')}
        b=self.make(a15_eval)
        b.rebalance('2026-11-19T20:00:00+00:00')
        self.assertEqual(b.fill_count,2)
        self.assertEqual(b.eval_count,3) # pre-order + after each simulated fill
        self.assertEqual(len(calls),3)
        self.assertAlmostEqual(float(b.state().equity),1200,places=3)
        b.rebalance('2026-11-19T20:00:00+00:00')
        self.assertEqual(b.fill_count,2) # no duplicate trades

    def test_reject_overleveraged(self):
        b=self.make(lambda *args: {'GDXU':1, 'BIL':1})
        with self.assertRaises(ValueError): b.rebalance('2026-11-19T20:00:00+00:00')
        self.assertEqual(b.fill_count,0)

    def test_reject_missing_price(self):
        b=self.make(lambda *args: {'GDXU':.5,'MISSING':.5})
        with self.assertRaises(ValueError): b.rebalance('2026-11-19T20:00:00+00:00')
        self.assertEqual(b.fill_count,0)

    def test_reject_stale_quote(self):
        b=self.make(lambda *args: {'GDXU':1})
        with self.assertRaises(ValueError): b.rebalance('2026-11-19T20:10:00+00:00')
        self.assertEqual(b.fill_count,0)

    def test_rejects_mismatched_initial_capital_on_ledger_reuse(self):
        b=self.make(lambda *args: {'BIL':1})
        path=b.ledger.db.execute('PRAGMA database_list').fetchone()[2]
        with self.assertRaisesRegex(ValueError, 'capital does not match'):
            PaperFillBot(str(SRC),path,lambda *args: {'BIL':1}, initial_cash='10000')

    def test_restart_restores_fill_ledger(self):
        b=self.make(lambda *args:{'GDXU':.5,'BIL':.5})
        b.rebalance('2026-11-19T20:00:00+00:00')
        path=b.ledger.db.execute('PRAGMA database_list').fetchone()[2]
        c=PaperFillBot(str(SRC),path,lambda *args:{'GDXU':.5,'BIL':.5})
        c.set_prices({'GDXU':50,'BIL':100},'2026-11-19T20:00:00+00:00')
        self.assertEqual(c.positions,b.positions)
        self.assertEqual(c.cash,b.cash)

if __name__ == '__main__': unittest.main()