"""Read-only Alpaca equity data client. NO trading/order endpoints."""
from __future__ import annotations
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
DATA='https://data.alpaca.markets'

class AlpacaDataError(RuntimeError):pass

class AlpacaMarketData:
    def __init__(self,feed='iex',key=None,secret=None,quote_fallback=False,
                 max_quote_spread_pct=1.0,max_quote_age_s=120):
        if feed not in ('iex','sip'):raise ValueError('Alpaca feed must be iex or sip')
        if max_quote_spread_pct<=0 or max_quote_spread_pct>5:
            raise ValueError('Quote spread cap must be >0% and <=5%')
        if max_quote_age_s<=0 or max_quote_age_s>600:
            raise ValueError('Quote age cap must be >0 and <=600 seconds')
        self.feed=feed
        self.quote_fallback=bool(quote_fallback)
        self.max_quote_spread_pct=float(max_quote_spread_pct)
        self.max_quote_age_s=float(max_quote_age_s)
        self.key=key or os.environ.get('APCA_API_KEY_ID')
        self.secret=secret or os.environ.get('APCA_API_SECRET_KEY')
        if not self.key or not self.secret:raise AlpacaDataError('Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY (data credentials)')

    def _get(self,path,params=None):
        url=DATA+path
        if params:url+='?'+urllib.parse.urlencode(params)
        req=urllib.request.Request(url,headers={'APCA-API-KEY-ID':self.key,
                        'APCA-API-SECRET-KEY':self.secret,'Accept':'application/json'},method='GET')
        try:
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        except Exception as e:
            # Do not print HTTP error bodies: some vendors include account metadata.
            raise AlpacaDataError(f'Alpaca READ-only data request failed ({path}): {type(e).__name__}: {str(e)[:140]}') from e

    def history(self,symbols,session_date,lookback_days=1450):
        today=dt.date.fromisoformat(session_date)
        params={'symbols':','.join(symbols),'timeframe':'1Day',
                'start':str(today-dt.timedelta(days=lookback_days)),
                'end':str(today),'limit':10000,'feed':self.feed,'adjustment':'split',
                'sort':'asc'}
        all_data={s:{} for s in symbols}
        pages=0
        while True:
            r=self._get('/v2/stocks/bars',params)
            pages+=1
            if pages>200:raise AlpacaDataError('Historical pagination exceeded safety limit')
            for sym,bars in r.get('bars',{}).items():
                if sym not in all_data:continue
                for b in bars:
                    stamp=dt.datetime.fromisoformat(b['t'].replace('Z','+00:00')).astimezone(NY)
                    day=stamp.date().isoformat()
                    if day>=session_date:continue  # never use today's uncompleted daily bar
                    all_data[sym][day]=float(b['c'])
            token=r.get('next_page_token')
            if not token:break
            params['page_token']=token
        return all_data

    def snapshot(self,symbols,session_date):
        """Return session-date IEX/SIP prices and source timestamps.

        By default use only latest actual trades. Optional quote fallback uses
        an IEX/SIP best-bid/ask midpoint ONLY when a real trade print is
        missing or older than 60 seconds. Quote must be <=max_quote_age_s
        old with a <=max_quote_spread_pct spread and positive, uncrossed bid/ask.
        Midpoint-based indicators and fills are research approximations, NOT
        Composer-identical closing prices or executable broker fills.

        Never forward-fill yesterday's quote or invent a current daily price.
        """
        prices={};ages={};times={}
        now=dt.datetime.now(dt.timezone.utc)

        def timestamp(value):
            if not value:return None
            try:
                t=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
                if t.tzinfo is None:return None
            except (ValueError,TypeError):
                return None
            if t.astimezone(NY).date().isoformat()!=session_date:return None
            age=(now-t).total_seconds()
            if age<-10:return None
            return t,age

        for offset in range(0,len(symbols),50):
            group=symbols[offset:offset+50]
            r=self._get('/v2/stocks/snapshots',{'symbols':','.join(group),'feed':self.feed})
            for sym,v in r.get('snapshots',{}).items():
                if sym not in group or not isinstance(v,dict):continue
                trade=v.get('latestTrade') or {}
                quote=v.get('latestQuote') or {}
                trade_data=None
                stamp=timestamp(trade.get('t'))
                if stamp:
                    try:
                        trade_price=float(trade.get('p',0))
                        if 0<trade_price<float('inf'):
                            trade_data=(trade_price,stamp[0],stamp[1])
                    except (ValueError,TypeError):pass

                quote_data=None
                if self.quote_fallback:
                    stamp=timestamp(quote.get('t'))
                    if stamp and stamp[1]<=self.max_quote_age_s:
                        try:
                            bid=float(quote.get('bp',0))
                            ask=float(quote.get('ap',0))
                            if bid>0 and ask>=bid and ask<float('inf'):
                                mid=(bid+ask)/2
                                spread_pct=100*(ask-bid)/mid
                                if spread_pct<=self.max_quote_spread_pct:
                                    quote_data=(mid,stamp[0],stamp[1])
                        except (ValueError,TypeError):pass

                chosen=trade_data
                if quote_data is not None and (trade_data is None or trade_data[2]>60):
                    chosen=quote_data
                if chosen is None:continue
                price,t,age=chosen
                prices[sym]=price
                ages[sym]=age
                times[sym]=t.isoformat()
        return prices,ages,times
