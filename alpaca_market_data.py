"""Alpaca read-only historical and snapshot data. NO order/broker endpoints.

Feed modes:
    iex:  IEX-only (limited-venue real-time data).
    sip:  SIP-only (requires entitlement for live snapshots).
    both: SIP preferred for current prices; per-symbol IEX fallback.
          History prefers completed SIP bars, filling gaps from IEX.
          Only HTTP 403 access denials allow fallback; rate limiting and
          other upstream errors are surfaced rather than hidden.

The optional quote-midpoint fallback is strictly a PAPER RESEARCH estimate,
not a broker-executable fill or an exact Composer daily-bar evaluation.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
DATA = 'https://data.alpaca.markets'


class AlpacaDataError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AlpacaMarketData:
    def __init__(self, feed='iex', key=None, secret=None, quote_fallback=False,
                 max_quote_spread_pct=1.0, max_quote_age_s=120):
        if feed not in ('iex', 'sip', 'both'):
            raise ValueError('Alpaca feed must be iex, sip or both')
        if max_quote_spread_pct <= 0 or max_quote_spread_pct > 5:
            raise ValueError('Quote spread cap must be >0% and <=5%')
        if max_quote_age_s <= 0 or max_quote_age_s > 600:
            raise ValueError('Quote age cap must be >0 and <=600 seconds')
        self.feed = feed
        self.quote_fallback = bool(quote_fallback)
        self.max_quote_spread_pct = float(max_quote_spread_pct)
        self.max_quote_age_s = float(max_quote_age_s)
        self.key = key or os.environ.get('APCA_API_KEY_ID')
        self.secret = secret or os.environ.get('APCA_API_SECRET_KEY')
        if not self.key or not self.secret:
            raise AlpacaDataError('Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY')
        # SIP historical access can differ from live snapshot entitlement.
        self.sip_live_allowed = None  # None = unknown, False = 403 denied
        self.sip_history_allowed = None
        self.last_sources = {}
        self.last_feed_status = []
        self.last_history_sources = {}

    def _get(self, path, params=None):
        url = DATA + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={'APCA-API-KEY-ID': self.key,
                          'APCA-API-SECRET-KEY': self.secret,
                          'Accept': 'application/json'}, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Never print credential-bearing headers or provider response bodies.
            raise AlpacaDataError(
                f'Alpaca read-only {path} failed: HTTP {exc.code}',
                status=exc.code) from exc
        except Exception as exc:
            raise AlpacaDataError(
                f'Alpaca read-only {path} failed: {type(exc).__name__}') from exc

    def _history_one(self, symbols, session_date, lookback_days, source):
        day = dt.date.fromisoformat(session_date)
        params = {
            'symbols': ','.join(symbols),
            'timeframe': '1Day',
            'start': str(day - dt.timedelta(days=lookback_days)),
            'end': session_date,
            'limit': 10000,
            'feed': source,
            'adjustment': 'split',
            'sort': 'asc',
        }
        result = {s: {} for s in symbols}
        pages = 0
        while True:
            data = self._get('/v2/stocks/bars', params)
            pages += 1
            if pages > 200:
                raise AlpacaDataError('Historical pagination exceeded 200 pages')
            for sym, bars in data.get('bars', {}).items():
                if sym not in result:
                    continue
                for bar in bars:
                    stamp = dt.datetime.fromisoformat(
                        bar['t'].replace('Z', '+00:00')).astimezone(NY)
                    bar_day = stamp.date().isoformat()
                    if bar_day >= session_date:
                        continue  # never use current incomplete daily bar
                    price = float(bar['c'])
                    if 0 < price < float('inf'):
                        result[sym][bar_day] = price
            token = data.get('next_page_token')
            if not token:
                break
            params['page_token'] = token
        return result

    def history(self, symbols, session_date, lookback_days=1450):
        if self.feed != 'both':
            self.last_history_sources = {self.feed: 'primary'}
            return self._history_one(symbols, session_date, lookback_days, self.feed)

        self.last_history_sources = {}
        try:
            primary = self._history_one(symbols, session_date, lookback_days, 'sip')
            self.sip_history_allowed = True
            self.last_history_sources['sip'] = 'available'
        except AlpacaDataError as exc:
            if exc.status != 403:
                raise
            self.sip_history_allowed = False
            self.last_history_sources['sip'] = 'HTTP 403: historical SIP not permitted'
            primary = {symbol: {} for symbol in symbols}

        # Fill SIP historical gaps with IEX; do not overwrite SIP prices.
        # This can produce mixed-feed historical series and is NOT Composer parity.
        secondary = self._history_one(symbols, session_date, lookback_days, 'iex')
        self.last_history_sources['iex'] = 'available'
        filled = 0
        for sym in symbols:
            for day, price in secondary.get(sym, {}).items():
                if day not in primary[sym]:
                    primary[sym][day] = price
                    filled += 1
        self.last_history_sources['iex_filled_missing_bars'] = filled
        return primary

    @staticmethod
    def _stamp(value, session_date, now):
        if not value:
            return None
        try:
            t = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if t.tzinfo is None:
                return None
        except (ValueError, TypeError):
            return None
        if t.astimezone(NY).date().isoformat() != session_date:
            return None
        age = (now - t).total_seconds()
        if age < -10:
            return None
        return t, age

    def _choose_snapshot(self, sample, session_date, now):
        if not isinstance(sample, dict):
            return None
        trade = sample.get('latestTrade') or {}
        quote = sample.get('latestQuote') or {}
        trade_data = None
        stamp = self._stamp(trade.get('t'), session_date, now)
        if stamp:
            try:
                price = float(trade.get('p', 0))
                if 0 < price < float('inf'):
                    trade_data = (price, stamp[0], stamp[1], 'trade')
            except (ValueError, TypeError):
                pass

        quote_data = None
        if self.quote_fallback:
            stamp = self._stamp(quote.get('t'), session_date, now)
            if stamp and stamp[1] <= self.max_quote_age_s:
                try:
                    bid = float(quote.get('bp', 0))
                    ask = float(quote.get('ap', 0))
                    if bid > 0 and ask >= bid and ask < float('inf'):
                        midpoint = (bid + ask) / 2
                        if 100 * (ask - bid) / midpoint <= self.max_quote_spread_pct:
                            quote_data = (midpoint, stamp[0], stamp[1], 'quote_midpoint')
                except (ValueError, TypeError):
                    pass
        selected = trade_data
        if quote_data is not None and (trade_data is None or trade_data[2] > 60):
            selected = quote_data
        # Do not let a stale SIP print mask fresh IEX coverage.
        if selected and selected[2] <= self.max_quote_age_s:
            return selected
        return None

    def snapshot(self, symbols, session_date):
        """Prefer real-time SIP; fetch IEX only for missing SIP symbols.

        Live SIP failures caused by 403 are remembered for the process lifetime
        (no repeated requests every 15 seconds). Other errors fail closed.
        Returns price, age, and timestamp dictionaries as before.
        Source labels are separately exposed in last_sources/last_feed_status.
        """
        prices, ages, times = {}, {}, {}
        self.last_sources = {}
        self.last_feed_status = []
        sources = ('sip', 'iex') if self.feed == 'both' else (self.feed,)
        now = dt.datetime.now(dt.timezone.utc)
        for source in sources:
            if source == 'sip' and self.feed == 'both' and self.sip_live_allowed is False:
                self.last_feed_status.append('SIP live: unavailable (previous HTTP 403)')
                continue
            remaining = [sym for sym in symbols if sym not in prices]
            if not remaining:
                break
            source_found = 0
            denied = False
            for offset in range(0, len(remaining), 50):
                group = remaining[offset:offset + 50]
                try:
                    data = self._get('/v2/stocks/snapshots',
                                     {'symbols': ','.join(group), 'feed': source})
                except AlpacaDataError as exc:
                    if source == 'sip' and self.feed == 'both' and exc.status == 403:
                        self.sip_live_allowed = False
                        denied = True
                        self.last_feed_status.append(
                            'SIP live: HTTP 403; IEX fallback enabled; check SIP subscription')
                        break
                    raise
                if source == 'sip':
                    self.sip_live_allowed = True
                for symbol, sample in data.get('snapshots', {}).items():
                    if symbol not in group or symbol in prices:
                        continue
                    selected = self._choose_snapshot(sample, session_date, now)
                    if selected is None:
                        continue
                    price, stamp, age, price_type = selected
                    prices[symbol] = price
                    ages[symbol] = age
                    times[symbol] = stamp.isoformat()
                    self.last_sources[symbol] = f'{source}:{price_type}'
                    source_found += 1
            if not denied:
                self.last_feed_status.append(f'{source.upper()} live: {source_found} symbols')
        return prices, ages, times
