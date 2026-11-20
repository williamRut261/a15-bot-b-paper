"""Standalone, local PAPER-ONLY A15 Bot B: intraday and after-every-fill.

Zero broker trading/write calls. Real-time market-data reads are optional
network requests using the user's Alpaca data credentials. Live orders are
not supported. This is not a verified Composer-identical indicator engine.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from decimal import Decimal
from zoneinfo import ZoneInfo

from a15_fill_bot import PaperFillBot,dec
from restore_strategy import ensure_strategy
from a15_realtime_eval import RealtimeA15Evaluator,IncompleteMarketData,collect_symbols
from alpaca_market_data import AlpacaMarketData,NY


class RealtimePaperBot(PaperFillBot):
    def __init__(self,*args,min_order_dollars='1',min_order_weight_bps='50',**kwargs):
        super().__init__(*args,**kwargs)
        self.min_order_dollars=dec(min_order_dollars)
        self.min_weight=dec(min_order_weight_bps)/dec(10000)
        if self.min_order_dollars<0 or self.min_weight<0:raise ValueError('Min orders must be positive')

    def _orders(self,weights):
        orders=super()._orders(weights)
        cutoff=max(self.min_order_dollars,self.state().equity*self.min_weight)
        return [(symbol,qty) for symbol,qty in orders if abs(qty*self.prices[symbol])>=cutoff]

    def paper_equity(self):
        return self.state().equity

    def fills_today(self,session_date):
        # Ledger is tiny on paper; authoritative previous-day state is persisted.
        today=dt.date.fromisoformat(session_date)
        rows=self.ledger.db.execute("SELECT at_utc,payload FROM events WHERE event_type='fill'").fetchall()
        return sum(1 for stamp,payload in rows if dt.datetime.fromisoformat(
            json.loads(payload).get('as_of',stamp)).astimezone(NY).date()==today)

    def _paper_fill(self,symbol,qty):
        ok=super()._paper_fill(symbol,qty)
        return ok


def arg_parser():
    p=argparse.ArgumentParser(description='A15 BOT B: stand-alone LIVE-DATA/LOCAL-PAPER, intraday+on-fill simulator')
    p.add_argument('--strategy',default=str(Path(__file__).with_name('original_a15.json')))
    p.add_argument('--ledger',default='a15_b_realtime_paper_1200.sqlite')
    p.add_argument('--capital',default='1200')
    p.add_argument('--feed',choices=['iex','sip'],default='iex',help='IEX free limited venue; SIP consolidated when entitled')
    p.add_argument('--poll-seconds',type=int,default=15)
    p.add_argument('--max-staleness-seconds',type=int,default=600)
    p.add_argument('--max-fills-per-day',type=int,default=80)
    p.add_argument('--min-order-dollars',default='1')
    p.add_argument('--min-order-weight-bps',default='50')
    p.add_argument('--slippage-bps',default='5')
    p.add_argument('--commission-bps',default='0')
    p.add_argument('--start-at',default='09:35',help='America/New_York')
    p.add_argument('--stop-at',default='15:55',help='America/New_York')
    p.add_argument('--once',action='store_true',help='One market-data fetch and evaluation, then exit')
    p.add_argument('--observe-only',action='store_true',help='Compute signals but never fill paper orders')
    p.add_argument('--allow-unverified-parity',action='store_true',help='Required to simulate trades: independent Composer group-ranking parity is not established')
    return p


def same_market_hours(now,start,stop):
    hhmm=now.strftime('%H:%M')
    return now.weekday()<5 and start<=hhmm<=stop


def format_state(bot):
    try:equity=str(bot.paper_equity())
    except ValueError:equity='unknown (missing quotes)'
    return {'equity':equity,'cash':str(bot.cash),
             'positions':{k:str(v) for k,v in bot.positions.items()},
             'fills_this_process':bot.fill_count,'evaluations_this_process':bot.eval_count}


def main(argv=None):
    a=arg_parser().parse_args(argv)
    if a.poll_seconds<3:raise SystemExit('Safety: --poll-seconds must be >= 3')
    if a.max_fills_per_day<=0 or a.max_fills_per_day>300:raise SystemExit('Daily fill cap must be 1..300')
    if not a.allow_unverified_parity and not a.observe_only:
        raise SystemExit('Independent A15 parity has not been verified. Add --observe-only to inspect signals or --allow-unverified-parity for LOCAL PAPER fills (no broker API).')
    if Path(a.strategy).resolve()==Path(__file__).with_name('original_a15.json').resolve():
        ensure_strategy()
    strategy=json.loads(Path(a.strategy).read_text())
    syms=collect_symbols(strategy)
    feed=AlpacaMarketData(feed=a.feed)
    bot=None; evaluator=None; session=None
    stop=False
    def stop_requested(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGINT,stop_requested)
    signal.signal(signal.SIGTERM,stop_requested)
    print(json.dumps({'status':'STARTED','mode':'OBSERVE_ONLY' if a.observe_only else 'LOCAL_PAPER',
         'network_access':'market-data-GET-only','live_orders_allowed':False,
         'strategy':strategy['name'],'data_feed':a.feed,'symbols_needed':len(syms)}),flush=True)
    while not stop:
        now=dt.datetime.now(NY)
        local_day=now.date().isoformat()
        if not same_market_hours(now,a.start_at,a.stop_at) and not a.once:
            time.sleep(min(60,a.poll_seconds));continue
        try:
            if session!=local_day:
                history=feed.history(syms,local_day)
                evaluator=RealtimeA15Evaluator(strategy,history)
                session=local_day
                print(json.dumps({'event':'history_loaded','session':local_day,
                       'days':len(evaluator.dates),'symbols':len(syms)}),flush=True)
                if bot is None:
                    bot=RealtimePaperBot(a.strategy,a.ledger,
                        lambda when,state,original:evaluator.evaluate(),
                        initial_cash=a.capital,slippage_bps=a.slippage_bps,
                        commission_bps=a.commission_bps,
                        max_quote_age_s=a.max_staleness_seconds,
                        min_order_dollars=a.min_order_dollars,
                        min_order_weight_bps=a.min_order_weight_bps)
            prices,ages,times=feed.snapshot(syms,local_day)
            # Snapshot messages can contain yesterday's trade: never ffill it.
            # Do not silently use a prior close for missing live symbols.
            # The evaluator raises if a missing symbol is actually needed.
            evaluator.update_intraday(local_day,prices)
            allocation=evaluator.evaluate()
            # Only accessed symbols (conditions, ranking candidates, etc.)
            # need a recent signal tick, but all targeted/held symbols must too.
            required=evaluator.accessed | set(allocation) | (set(bot.positions) if bot else set())
            stale=sorted(s for s in required if ages.get(s,float('inf'))>a.max_staleness_seconds)
            if stale:raise IncompleteMarketData(f'Stale signal/target symbols ({len(stale)}): {stale[:12]}')
            when=dt.datetime.now(dt.timezone.utc).isoformat()
            bot.set_prices(prices,when)
            if not a.observe_only:
                fills_before=bot.fill_count
                # Day cap accounts for previous restarts based on ledger timestamps;
                # virtual order-fill loop still enforces max_fills-per-rebalance.
                daily_fills=bot.fills_today(local_day)
                if daily_fills>=a.max_fills_per_day:
                    bot.ledger.append('halt',{'reason':'daily fill cap','day':local_day,'count':daily_fills})
                    raise RuntimeError('Daily paper fill cap reached; no more trades today')
                bot.max_fills=min(35,a.max_fills_per_day-daily_fills)
                bot.rebalance(when,reason='realtime_market_update')
                new_fills=bot.fill_count-fills_before
            else:new_fills=0
            print(json.dumps({'event':'EVALUATION','time':when,
                  'weights':{k:round(v,8) for k,v in allocation.items()},
                  'freshness_max_s':round(max(ages[s] for s in required),1),
                  'paper_fills':new_fills,**format_state(bot)}),flush=True)
        except Exception as exc:
            print(json.dumps({'event':'NO_TRADE','time':dt.datetime.now(dt.timezone.utc).isoformat(),
               'error':f'{type(exc).__name__}: {str(exc)[:400]}'}),file=sys.stderr,flush=True)
        if a.once:break
        time.sleep(a.poll_seconds)
    return 0

if __name__=='__main__':sys.exit(main())