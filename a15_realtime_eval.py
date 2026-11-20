"""Independent intraday evaluator for the original Composer A15 JSON.

Implements every node *type* used in the supplied master and all listed
indicator functions. Results are a transparent research replica, NOT Composer
parity: synthetic historical group rankings, RSI smoothing, tie-breaking and
intraday partial-bar treatment must be checked against Composer allocations.
No orders, network, credentials or global mutable strategy state here.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

class IncompleteMarketData(ValueError): pass
class UnsupportedNode(ValueError): pass


def collect_symbols(root):
    symbols=set()
    def walk(n):
        if n.get('step')=='asset': symbols.add(n['ticker'])
        if n.get('step')=='if-child':
            for k in ('lhs-val','rhs-val'):
                val=n.get(k)
                if isinstance(val,str) and val.isupper() and val.replace('.','').isalpha() and len(val)<=7:
                    symbols.add(val)
        for c in n.get('children',[]):walk(c)
    walk(root)
    return sorted(symbols)


def weight_of(n):
    w=n.get('weight')
    if not w:return None
    return float(w['num'])/float(w['den'])


def merge(results, factors):
    result=defaultdict(float)
    for weights, frac in zip(results,factors):
        for sym,amount in weights.items():result[sym]+=frac*amount
    return dict(result)


class RealtimeA15Evaluator:
    """history: {symbol: {ISO date: split-adjusted daily closing price}}.

    Last historical date must be strictly BEFORE the current NY session date;
    `update_intraday` creates one changing incomplete day, never future close.
    """
    def __init__(self,strategy:dict,history:dict,min_days:int=205):
        self.root=strategy
        self.symbols=collect_symbols(strategy)
        # Reference date union; per-security missing prices are NOT ffilled.
        hist={s:{str(k):float(v) for k,v in x.items()} for s,x in history.items()}
        if 'SPY' not in hist:raise IncompleteMarketData('SPY history required for session calendar')
        self.dates=sorted(hist['SPY'])
        if len(self.dates)<min_days:raise IncompleteMarketData(f'{len(self.dates)} trading days; need {min_days}')
        self.close={s:[hist.get(s,{}).get(d) for d in self.dates] for s in self.symbols}
        self.base_n=len(self.dates)
        self.session=None
        self.live_index=None
        self.active_prices={}
        self._history_alloc=self._historical_allocation
        self._history_score=self._historical_score
        self._history_metric=self._historical_metric
        self.accessed=set()
        self._build_nodes()

    def _build_nodes(self):
        seen=set()
        def traverse(n):
            key=n['id']
            if key in seen:pass
            seen.add(key)
            for c in n.get('children',[]):traverse(c)
        traverse(self.root)
        self.node_count=len(seen)

    def update_intraday(self,session_date:str,prices:dict[str,float]):
        date.fromisoformat(session_date)
        if session_date<=self.dates[-1]:
            raise IncompleteMarketData('Current session must be later than last completed historical bar')
        if self.session!=session_date:
            self.session=session_date
            self.live_index=self.base_n
        self.active_prices={str(k):float(v) for k,v in prices.items()}
        if not all(math.isfinite(v) and v>0 for v in self.active_prices.values()):
            raise IncompleteMarketData('Invalid live price')
        self.accessed.clear()

    def price(self,sym,i):
        self.accessed.add(sym)
        if sym not in self.close:
            raise IncompleteMarketData(f'Missing ticker {sym}')
        if i==self.base_n:
            v=self.active_prices.get(sym)
        elif i>=0 and i<self.base_n:v=self.close[sym][i]
        else:v=None
        if v is None or v<=0:raise IncompleteMarketData(f'Missing price for {sym} on index {i}')
        return v

    def _returns(self,sym,i,window):
        if window<=0:raise ValueError('bad window')
        return [self.price(sym,j)/self.price(sym,j-1)-1 for j in range(i-window+1,i+1)]

    def metric(self,fn,sym,i,window=None):
        if i<self.base_n:
            return self._history_metric(fn,sym,i,window)
        return self._calc_metric(fn,sym,i,window)

    @lru_cache(maxsize=500000)
    def _historical_metric(self,fn,sym,i,window):
        return self._calc_metric(fn,sym,i,window)

    def _calc_metric(self,fn,sym,i,window):
        if fn=='current-price':return self.price(sym,i)
        if window is None:raise UnsupportedNode(f'Indicator {fn} without window')
        n=int(window)
        if fn=='cumulative-return':return 100*(self.price(sym,i)/self.price(sym,i-n)-1)
        if fn=='moving-average-price':return sum(self.price(sym,j) for j in range(i-n+1,i+1))/n
        if fn=='moving-average-return':return 100*statistics.mean(self._returns(sym,i,n))
        if fn=='standard-deviation-return':return 100*statistics.stdev(self._returns(sym,i,n)) if n>1 else 0.0
        if fn=='relative-strength-index':
            # Wilder RSI, seeded with a simple average over the first `n` changes.
            # Warmup begins at the first n available sessions of this ticker.
            hist=self.close.get(sym)
            if hist is None:raise IncompleteMarketData(f'Missing {sym}')
            first=next((j for j,p in enumerate(hist) if p is not None),None)
            if first is None or i<first+n:raise IncompleteMarketData(f'RSI warmup missing for {sym}')
            diffs=[self.price(sym,j)-self.price(sym,j-1) for j in range(first+1,first+n+1)]
            gain=sum(max(0,x) for x in diffs)/n
            loss=sum(max(0,-x) for x in diffs)/n
            for j in range(first+n+1,i+1):
                d=self.price(sym,j)-self.price(sym,j-1)
                gain=(gain*(n-1)+max(0,d))/n
                loss=(loss*(n-1)+max(0,-d))/n
            if loss==0:return 100.0 if gain>0 else 50.0
            return 100-100/(1+gain/loss)
        raise UnsupportedNode(f'Unsupported indicator {fn}')

    def _if_pass(self,node,i):
        if node.get('is-else-condition?'):return True
        lhsfn=node.get('lhs-fn');rhsfn=node.get('rhs-fn')
        if not lhsfn:raise UnsupportedNode('Missing if condition')
        lw=node.get('lhs-fn-params',{}).get('window',node.get('lhs-window-days'))
        lv=self.metric(lhsfn,node['lhs-val'],i,lw)
        if node.get('rhs-fixed-value?',not bool(rhsfn)):
            rv=float(node['rhs-val'])
        else:
            rw=node.get('rhs-fn-params',{}).get('window',node.get('rhs-window-days'))
            rv=self.metric(rhsfn,node['rhs-val'],i,rw)
        return lv>rv if node.get('comparator')=='gt' else lv<rv if node.get('comparator')=='lt' else self._bad_comp(node)

    @staticmethod
    def _bad_comp(node):raise UnsupportedNode(f"Unknown comparator {node.get('comparator')}")

    def _node_returns(self,node,i,window):
        # Synthetic virtual group P&L: each prior completed day's allocation
        # is marked from that close to the next day's close (no lookahead).
        ret=[]
        for end in range(i-window+1,i+1):
            weights=self.allocation(node,end-1)
            daily=sum(w*(self.price(s,end)/self.price(s,end-1)-1) for s,w in weights.items())
            ret.append(daily)
        return ret

    @lru_cache(maxsize=150000)
    def _historical_score(self,node_id,i,fn,window):
        return self._score(self.nodes_by_id[node_id],i,fn,window)

    def score(self,node,i,fn,window):
        if i<self.base_n:return self._historical_score(node['id'],i,fn,window)
        return self._score(node,i,fn,window)

    def _score(self,node,i,fn,window):
        if node['step']=='asset':return self.metric(fn,node['ticker'],i,window)
        # This is the principal independent approximation needing parity audit.
        arr=self._node_returns(node,i,int(window))
        if fn=='moving-average-return':return 100*statistics.mean(arr)
        if fn=='standard-deviation-return':return 100*statistics.stdev(arr)
        if fn=='cumulative-return':return 100*(math.prod(1+x for x in arr)-1)
        raise UnsupportedNode(f'Unsupported nested group metric {fn}')

    def allocation(self,node,i):
        if i<self.base_n:return self._historical_allocation(node['id'],i)
        return self._calc_allocation(node,i)

    @lru_cache(maxsize=150000)
    def _historical_allocation(self,node_id,i):
        return self._calc_allocation(self.nodes_by_id[node_id],i)

    def _calc_allocation(self,n,i):
        typ=n['step'];children=n.get('children',[])
        if typ=='asset':return {n['ticker']:1.0}
        if typ in ('root','group','if-child'):
            if not children:raise UnsupportedNode(f'{typ} had no children')
            if len(children)==1:return self.allocation(children[0],i)
            # Composer branches with direct sibling assets are implicit equal weights.
            return merge([self.allocation(c,i) for c in children],[1/len(children)]*len(children))
        if typ=='if':
            for child in children:
                if self._if_pass(child,i):return self.allocation(child,i)
            raise UnsupportedNode('if chain had no matching branch')
        if typ in ('wt-cash-equal','wt-cash-specified','wt-inverse-vol'):
            if not children:raise UnsupportedNode('Empty weight node')
            if typ=='wt-cash-equal':facts=[1/len(children)]*len(children)
            elif typ=='wt-cash-specified':
                if any(weight_of(c) is None for c in children):raise UnsupportedNode('Specified weight missing')
                facts=[weight_of(c) for c in children]
                if not 0.999<=sum(facts)<=1.001:raise UnsupportedNode('Specified weight sum != 100%')
            else:
                window=int(n['window-days'])
                sigmas=[self.score(c,i,'standard-deviation-return',window) for c in children]
                inv=[1/max(x,1e-8) for x in sigmas]
                facts=[x/sum(inv) for x in inv]
            return merge([self.allocation(c,i) for c in children],facts)
        if typ=='filter':
            fn=n['sort-by-fn'];win=n.get('sort-by-fn-params',{}).get('window',n.get('sort-by-window-days'))
            if win is None:raise UnsupportedNode('Filter needs window')
            scored=[(self.score(c,i,fn,int(win)),j,c) for j,c in enumerate(children)]
            reverse=n.get('select-fn')=='top'
            if n.get('select-fn') not in ('top','bottom'):raise UnsupportedNode('Filter select')
            # Deterministic ties by original child order (not claimed Composer parity).
            scored.sort(key=lambda t:(-t[0] if reverse else t[0],t[1]))
            take=int(n['select-n'])
            if not 1<=take<=len(children):raise UnsupportedNode('Filter count invalid')
            picked=[z[2] for z in scored[:take]]
            return merge([self.allocation(c,i) for c in picked],[1/take]*take)
        raise UnsupportedNode(f'Unsupported node type: {typ}')

    def evaluate(self):
        if self.live_index is None:raise IncompleteMarketData('Call update_intraday first')
        self.nodes_by_id={}
        def walk(n):
            self.nodes_by_id[n['id']]=n
            for c in n.get('children',[]):walk(c)
        walk(self.root)
        weights=self.allocation(self.root,self.live_index)
        if not weights or any(not math.isfinite(x) or x<0 for x in weights.values()):raise ValueError('Invalid A15 target')
        total=sum(weights.values())
        if not .999 <= total <= 1.001:raise ValueError(f'A15 allocation sum {total} !=1')
        return weights


def load_history(path):
    data=json.loads(Path(path).read_text())
    if not isinstance(data,dict):raise ValueError('History JSON must be ticker -> date -> close')
    return data