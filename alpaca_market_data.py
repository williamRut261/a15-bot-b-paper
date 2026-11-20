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
    def __init__(self,feed='iex',key=None,secret=None):
        if feed not in ('iex','sip'):raise ValueError('Alpaca feed must be iex or sip')
        self.feed=feed
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
        """Return live prices and actual source timestamps, not fabricated 'now'.

        Only last trade data may update intraday signals. A data provider may
        return yesterday's last trade; caller MUST enforce freshness.
        """
        prices={};ages={};times={}
        now=dt.datetime.now(dt.timezone.utc)
        for offset in range(0,len(symbols),50):
            group=symbols[offset:offset+50]
            r=self._get('/v2/stocks/snapshots',{'symbols':','.join(group),'feed':self.feed})
            for sym,v in r.get('snapshots',{}).items():
                trade=v.get('latestTrade') or {}
                if not trade or not trade.get('p') or not trade.get('t'):continue
                t=dt.datetime.fromisoformat(trade['t'].replace('Z','+00:00'))
                if t.astimezone(NY).date().isoformat()!=session_date:continue
                age=(now-t).total_seconds()
                if age< -10:continue
                prices[sym]=float(trade['p'])
                ages[sym]=age
                times[sym]=t.isoformat()
        return prices,ages,times