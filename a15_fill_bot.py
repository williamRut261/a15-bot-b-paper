"""A15 Bot B: PAPER-ONLY, post-fill full-strategy re-evaluation.

This module does not place or permit live broker orders. For a faithful A15
forward test, supply an evaluator callable backed by the existing unchanged
A15 engine. The original Composer JSON is included for identity validation,
but its complex 2-of-3 ranking is NOT approximated here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable, Mapping

CENT = Decimal("0.01")
STEP = Decimal("0.0001")


def dec(value: object) -> Decimal:
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Non-finite price, weight or quantity")
    return x


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class State:
    cash: Decimal
    positions: dict[str, Decimal]
    prices: dict[str, Decimal]

    @property
    def equity(self) -> Decimal:
        return self.cash + sum((q * self.prices[t] for t, q in self.positions.items()), Decimal(0))


class Ledger:
    """Journal fill and decision events. SQLite is local-only."""

    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              at_utc TEXT NOT NULL, event_type TEXT NOT NULL,
              payload TEXT NOT NULL);
        """)
        self.db.commit()

    def append(self, kind: str, payload: dict):
        self.db.execute("INSERT INTO events (at_utc, event_type, payload) VALUES (?,?,?)",
                        (utc_now(), kind, json.dumps(payload, sort_keys=True, default=str)))
        self.db.commit()

    def events(self):
        return self.db.execute("SELECT event_type,payload FROM events ORDER BY seq").fetchall()


class PaperFillBot:
    """No external trading calls. One simulated fill -> one FULL A15 evaluation.

    The signal engine is an injected callable:
      evaluate(as_of:str, state:State, original_a15_json:dict) -> {symbol: target_weight}

    This interface intentionally requires the original ranking engine to be
    wired in; a GDXU-only approximation would invalidate the experiment.
    """

    def __init__(self, a15_json_path: str, ledger_path: str, evaluator: Callable,
                 initial_cash: str = "1200", slippage_bps: str = "1",
                 commission_bps: str = "0", max_fills_per_rebalance: int = 35,
                 max_quote_age_s: int = 30):
        self.a15_path = Path(a15_json_path)
        original_bytes = self.a15_path.read_bytes()
        self.strategy_sha = hashlib.sha256(original_bytes).hexdigest()
        self.original = json.loads(original_bytes)
        if self.original.get('step') != 'root' or 'children' not in self.original:
            raise ValueError("Not an original Composer A15 root JSON")
        self.evaluator = evaluator
        self.ledger = Ledger(ledger_path)
        self.slippage = dec(slippage_bps) / dec(10000)
        self.commission = dec(commission_bps) / dec(10000)
        if self.slippage < 0 or self.commission < 0:
            raise ValueError("Costs cannot be negative")
        self.max_fills = max_fills_per_rebalance
        self.max_quote_age_s = max_quote_age_s
        self.prices: dict[str, Decimal] = {}
        self.price_times: dict[str, dt.datetime] = {}
        self.positions: dict[str, Decimal] = {}
        self.cash = dec(initial_cash)
        previous = self.ledger.events()
        if previous:
            setup = json.loads(previous[0][1])
            if previous[0][0] != 'setup' or setup.get('strategy_sha') != self.strategy_sha:
                raise ValueError("Existing ledger belongs to a different strategy")
            if dec(setup['initial_cash']) != dec(initial_cash):
                raise ValueError("Existing paper ledger capital does not match --capital; use a new --ledger filename to start with the requested balance")
            self.cash = dec(setup['initial_cash'])
            for kind, raw in previous[1:]:
                x = json.loads(raw)
                if kind == 'fill':
                    sym,qty,price,fee=x['symbol'],dec(x['quantity']),dec(x['fill_price']),dec(x['fee'])
                    self.positions[sym] = self.positions.get(sym,Decimal(0)) + qty
                    if abs(self.positions[sym]) < STEP/2: del self.positions[sym]
                    self.cash -= qty * price + fee
        else:
            self.ledger.append('setup', {'strategy_sha': self.strategy_sha,
                                          'strategy_name': self.original.get('name'),
                                          'initial_cash':str(self.cash),
                                          'PAPER_ONLY': True})
        self.eval_count = 0
        self.fill_count = 0

    def state(self) -> State:
        missing = set(self.positions) - set(self.prices)
        if missing:
            raise ValueError(f"No price for existing positions: {sorted(missing)}")
        return State(self.cash, dict(self.positions), dict(self.prices))

    def set_prices(self, prices: Mapping[str, object], at_utc: str):
        stamp = dt.datetime.fromisoformat(at_utc.replace('Z','+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Market timestamps must have a timezone')
        for sym, raw in prices.items():
            p = dec(raw)
            if p <= 0: raise ValueError(f"Nonpositive price: {sym}")
            self.prices[sym] = p
            self.price_times[sym] = stamp

    def _validate_targets(self, targets: dict) -> dict[str, Decimal]:
        if not isinstance(targets, dict) or not targets:
            raise ValueError("A15 evaluator must return nonempty target-weight mapping")
        w = {str(t): dec(v) for t,v in targets.items()}
        if any(x < 0 or x > 1 for x in w.values()):
            raise ValueError("Expected long-only allocation weights between 0 and 1")
        if sum(w.values(), Decimal(0)) > Decimal('1.000001'):
            raise ValueError("A15 portfolio allocation exceeds 100%")
        missing = set(w) - set(self.prices)
        if missing: raise ValueError(f"No current price for target symbols: {sorted(missing)}")
        return w

    def _check_fresh(self, timestamp: str, targets: dict[str,Decimal]):
        now = dt.datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        for symbol in set(targets) | set(self.positions):
            if symbol not in self.price_times: raise ValueError(f'Missing price timestamp {symbol}')
            age = abs((now - self.price_times[symbol]).total_seconds())
            if age > self.max_quote_age_s:
                raise ValueError(f"Stale quote {symbol}: {age:.0f}s > {self.max_quote_age_s}s")

    def _orders(self, weights: dict[str,Decimal]) -> list[tuple[str,Decimal]]:
        s = self.state()
        eq = s.equity
        # Rebalance to target dollar holdings. Sell first to release virtual cash.
        sells=[]; buys=[]
        for sym in sorted(set(weights) | set(s.positions)):
            price=s.prices[sym]
            ideal=(eq * weights.get(sym,Decimal(0)) / price).quantize(STEP,rounding=ROUND_DOWN)
            have=s.positions.get(sym,Decimal(0))
            qty=ideal-have
            if abs(qty * price) < Decimal('1.00'): continue  # $1 virtual notional threshold
            (sells if qty < 0 else buys).append((sym,qty))
        return sells+buys

    def _paper_fill(self, symbol: str, qty: Decimal):
        mark=self.prices[symbol]
        fill_price=mark*(1+self.slippage if qty>0 else 1-self.slippage)
        fee=abs(qty*fill_price)*self.commission
        if qty>0:
            max_affordable=(self.cash/(fill_price*(1+self.commission))).quantize(STEP,rounding=ROUND_DOWN)
            qty=min(qty,max_affordable)
            if qty<=0 or qty*fill_price<1: return False
            fee=abs(qty*fill_price)*self.commission
        elif -qty > self.positions.get(symbol, Decimal(0)) + STEP/2:
            raise RuntimeError("Paper simulator cannot sell unowned shares")
        self.cash-=qty*fill_price+fee
        self.positions[symbol]=self.positions.get(symbol,Decimal(0))+qty
        if abs(self.positions[symbol])<STEP/2:del self.positions[symbol]
        self.fill_count+=1
        self.ledger.append('fill', {'symbol':symbol,'quantity':str(qty),
              'fill_price':str(fill_price),'fee':str(fee), 'mark':str(mark),
              'as_of':getattr(self,'_event_time',utc_now()),
              'note':'SIMULATED virtual fill, not an exchange or broker order'})
        return True

    def rebalance(self, as_of_utc: str, reason: str = 'daily'):
        """Run full A15 engine on daily event, AND after EACH paper fill.

        Fail closed if quote freshness, weights, or event-loop caps fail.
        """
        self._event_time=as_of_utc
        n=0
        seen=set()
        while True:
            state=self.state()
            targets=self._validate_targets(self.evaluator(as_of_utc,state,self.original))
            self.eval_count+=1
            self._check_fresh(as_of_utc, targets)
            self.ledger.append('decision', {'reason':reason,'as_of':as_of_utc,
                 'targets':{k:str(v) for k,v in targets.items()},
                 'equity':str(state.equity), 'cash':str(state.cash),
                 'positions':{k:str(v) for k,v in state.positions.items()}})
            orders=self._orders(targets)
            if not orders: return
            if n>=self.max_fills:
                self.ledger.append('halt', {'reason':'Maximum post-fill recalculations exceeded', 'fills':n})
                raise RuntimeError("Paper rebalance halted: order-fill loop")
            signature=(tuple(sorted((k,str(v)) for k,v in targets.items())),
                       tuple(sorted((k,str(v)) for k,v in state.positions.items())))
            if signature in seen:
                self.ledger.append('halt', {'reason':'Order-fill oscillation', 'fills':n})
                raise RuntimeError("Paper rebalance halted: oscillation")
            seen.add(signature)
            # ONE order, then full original-A15 reevaluation on its fill.
            sym,qty=orders[0]
            if not self._paper_fill(sym,qty):
                # A dollar of dust can remain because of costs; don't loop forever.
                self.ledger.append('skip', {'reason':'Insufficient simulated cash', 'symbol':sym})
                return
            n+=1
            reason='order_fill'


def demo_evaluator(as_of_utc: str, state: State, original_a15_json: dict) -> dict:
    """A deterministic SMOKE TEST ONLY. Not an A15 evaluator."""
    return {'GDXU':Decimal('.50'),'BIL':Decimal('.50')}


def load_evaluator(spec: str):
    mod, colon, fn = spec.partition(':')
    if not colon or not mod or not fn:
        raise ValueError('Use --evaluator module_name:function_name')
    callable_obj=getattr(importlib.import_module(mod),fn)
    if not callable(callable_obj): raise ValueError('Evaluator must be callable')
    return callable_obj


def main():
    p=argparse.ArgumentParser(description='A15 order-on-fill PAPER simulator (no real trades)')
    p.add_argument('--strategy', default='original_a15.json')
    p.add_argument('--ledger', default='a15_bot_b_paper.sqlite')
    p.add_argument('--evaluator', required=True,
                   help='Existing verified A15 evaluation hook, module:function; demo only: a15_fill_bot:demo_evaluator')
    p.add_argument('--cash',default='1200')
    p.add_argument('--slippage-bps',default='1')
    p.add_argument('--commission-bps',default='0')
    p.add_argument('--max-quote-age-s',type=int,default=30)
    args=p.parse_args()
    bot=PaperFillBot(args.strategy,args.ledger,load_evaluator(args.evaluator),
                     initial_cash=args.cash,slippage_bps=args.slippage_bps,
                     commission_bps=args.commission_bps,max_quote_age_s=args.max_quote_age_s)
    # JSONL events from an external read-only market-data stream. Only a
    # `daily` event executes scheduled rebalance. Fills trigger extra passes.
    for line in sys.stdin:
        if not line.strip(): continue
        msg=json.loads(line)
        if msg.get('type') not in ('daily','quote'): raise ValueError('Unknown feed message type')
        bot.set_prices(msg['prices'],msg['timestamp'])
        if msg['type']=='daily':
            bot.rebalance(msg['timestamp'])
            s=bot.state()
            print(json.dumps({'at':msg['timestamp'],'equity':str(s.equity),
                              'cash':str(s.cash),
                              'positions':{k:str(v) for k,v in s.positions.items()},
                              'evaluations':bot.eval_count,'paper_fills':bot.fill_count}),flush=True)

if __name__ == '__main__':
    main()