import datetime as dt
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from a15_realtime_eval import RealtimeA15Evaluator,collect_symbols,IncompleteMarketData
from a15_fill_bot import PaperFillBot
from run_realtime_bot_b import RealtimePaperBot

SRC=Path(__file__).resolve().parents[1]/'original_a15.json'
STRATEGY=json.loads(SRC.read_text())


def fake_history(seed=11,days=410):
    # synthetic completed trading days; NOT market performance.
    random.seed(seed)
    start=dt.date(2024,6,1)
    dates=[(start+dt.timedelta(days=i)).isoformat() for i in range(days*2) if
           (start+dt.timedelta(days=i)).weekday()<5][:days]
    prices={}
    for s in collect_symbols(STRATEGY):
        p=60.0;data={}
        for day in dates:
            p*=math.exp(random.gauss(.0003,.023))
            data[day]=p
        prices[s]=data
    return prices


class FullA15IntradayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=fake_history()
        cls.live={s:list(x.values())[-1] for s,x in cls.data.items()}

    def setup_engine(self):
        ev=RealtimeA15Evaluator(STRATEGY,self.data)
        self.assertEqual(len(ev.symbols),72)
        return ev

    def test_full_json_runs_without_placeholder(self):
        ev=self.setup_engine()
        ev.update_intraday('2026-11-20',self.live)
        target=ev.evaluate()
        self.assertGreaterEqual(len(target),1)
        self.assertAlmostEqual(sum(target.values()),1.0,places=5)

    def test_intraday_crash_and_recovery_change_full_targets(self):
        ev=self.setup_engine()
        ev.update_intraday('2026-11-20',self.live)
        base=ev.evaluate()
        self.assertAlmostEqual(sum(base.values()),1)
        crash=dict(self.live)
        crash['GDXU']=self.data['GDXU'][ev.dates[-2]]*.65
        ev.update_intraday('2026-11-20',crash)
        self.assertEqual(ev.evaluate(),{'BIL':1.0})
        ev.update_intraday('2026-11-20',self.live)
        self.assertEqual(ev.evaluate(),base)

    def test_one_day_three_evaluations_multiple_paper_orders(self):
        ev=self.setup_engine()
        def eval_target(*_):return ev.evaluate()
        with tempfile.TemporaryDirectory() as td:
            bot=RealtimePaperBot(str(SRC),str(Path(td)/'paper.sqlite'),eval_target,
                                 min_order_dollars='1',min_order_weight_bps='0',slippage_bps='0')
            ts='2026-11-20T16:00:00+00:00'
            ev.update_intraday('2026-11-20',self.live)
            bot.set_prices(self.live,ts)
            bot.rebalance(ts,'market_update')
            first=bot.fill_count
            self.assertGreater(first,0)
            crash=dict(self.live)
            crash['GDXU']=self.data['GDXU'][ev.dates[-2]]*.65
            ts='2026-11-20T16:05:00+00:00'
            ev.update_intraday('2026-11-20',crash)
            bot.set_prices(crash,ts)
            bot.rebalance(ts,'market_update')
            second=bot.fill_count
            self.assertGreater(second,first)
            ts='2026-11-20T16:10:00+00:00'
            ev.update_intraday('2026-11-20',self.live)
            bot.set_prices(self.live,ts)
            bot.rebalance(ts,'market_update')
            self.assertGreater(bot.fill_count,second)
            self.assertGreater(bot.eval_count,bot.fill_count)
            self.assertEqual(bot.fills_today('2026-11-20'),bot.fill_count)

    def test_missing_market_data_fails_closed(self):
        ev=self.setup_engine()
        s=dict(self.live)
        s.pop('GDXU')
        ev.update_intraday('2026-11-20',s)
        with self.assertRaises(IncompleteMarketData):ev.evaluate()

    def test_live_price_overrides_completed_bar_but_no_future_data(self):
        ev=self.setup_engine()
        ev.update_intraday('2026-11-20',self.live)
        self.assertEqual(ev.price('GDXU',ev.base_n),self.live['GDXU'])
        with self.assertRaises(IncompleteMarketData):ev.price('GDXU',ev.base_n+1)

if __name__=='__main__':unittest.main()