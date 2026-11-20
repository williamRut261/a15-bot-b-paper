"""Dual Alpaca market-data safety tests; no network or broker requests."""
import datetime as dt
import unittest

from alpaca_market_data import AlpacaDataError, AlpacaMarketData, NY


class DualFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        self.session = self.now.astimezone(NY).date().isoformat()
        self.calls = []
        self.data = AlpacaMarketData(
            feed='both', key='dummy-key', secret='dummy-secret',
            quote_fallback=True, max_quote_age_s=120)

    def trade(self, price, minutes=0):
        stamp = (self.now - dt.timedelta(minutes=minutes)).isoformat()
        return {'latestTrade': {'p': price, 't': stamp}}

    def quote(self, bid, ask):
        return {'latestQuote': {
            'bp': bid, 'ap': ask, 't': self.now.isoformat()}}

    def mock(self, sip, iex, sip_status=None):
        def get(path, params=None):
            source = params['feed']
            self.calls.append((path, source, params.get('symbols', '')))
            if source == 'sip' and sip_status:
                raise AlpacaDataError('SIP denied', status=sip_status)
            samples = sip if source == 'sip' else iex
            if path.endswith('/snapshots'):
                return {'snapshots': samples}
            if path.endswith('/bars'):
                return {'bars': samples, 'next_page_token': None}
            raise AssertionError(path)
        self.data._get = get

    def test_sip_is_preferred_when_all_symbols_covered(self):
        self.mock({'GDXU': self.trade(101), 'BIL': self.trade(92)},
                  {'GDXU': self.trade(99), 'BIL': self.trade(91)})
        prices, _, _ = self.data.snapshot(['GDXU', 'BIL'], self.session)
        self.assertEqual(prices, {'GDXU': 101, 'BIL': 92})
        self.assertTrue(all(src.startswith('sip:') for src in self.data.last_sources.values()))
        self.assertEqual([c[1] for c in self.calls], ['sip'])

    def test_iex_fills_only_missing_sip_symbols(self):
        self.mock({'BIL': self.trade(92)},
                  {'GDXU': self.trade(99), 'BIL': self.trade(91)})
        prices, _, _ = self.data.snapshot(['GDXU', 'BIL'], self.session)
        self.assertEqual(prices, {'BIL': 92, 'GDXU': 99})
        self.assertEqual(self.data.last_sources['BIL'], 'sip:trade')
        self.assertEqual(self.data.last_sources['GDXU'], 'iex:trade')
        self.assertEqual(self.calls[1][2], 'GDXU')

    def test_403_sip_downgrades_to_iex_without_repeated_denials(self):
        self.mock({}, {'GDXU': self.trade(99)}, sip_status=403)
        first, _, _ = self.data.snapshot(['GDXU'], self.session)
        second, _, _ = self.data.snapshot(['GDXU'], self.session)
        self.assertEqual(first, {'GDXU': 99})
        self.assertEqual(second, {'GDXU': 99})
        self.assertEqual([c[1] for c in self.calls], ['sip', 'iex', 'iex'])
        self.assertFalse(self.data.sip_live_allowed)
        self.assertTrue(any('403' in x for x in self.data.last_feed_status))

    def test_429_rate_limit_is_not_silently_hidden_by_iex(self):
        self.mock({}, {'GDXU': self.trade(99)}, sip_status=429)
        with self.assertRaises(AlpacaDataError) as e:
            self.data.snapshot(['GDXU'], self.session)
        self.assertEqual(e.exception.status, 429)
        self.assertEqual([c[1] for c in self.calls], ['sip'])

    def test_stale_sip_trade_is_replaced_by_fresh_iex(self):
        self.mock({'GDXU': self.trade(101, minutes=5)},
                  {'GDXU': self.trade(99)})
        prices, _, _ = self.data.snapshot(['GDXU'], self.session)
        self.assertEqual(prices['GDXU'], 99)
        self.assertEqual(self.data.last_sources['GDXU'], 'iex:trade')

    def test_sip_quote_midpoint_can_fill_missing_sip_print(self):
        self.mock({'GDXU': self.quote(99.8, 100.2)},
                  {'GDXU': self.trade(97)})
        prices, _, _ = self.data.snapshot(['GDXU'], self.session)
        self.assertEqual(prices['GDXU'], 100)
        self.assertEqual(self.data.last_sources['GDXU'], 'sip:quote_midpoint')

    def test_historical_sip_preferred_iex_fills_gaps(self):
        old = (self.now.astimezone(NY).date() - dt.timedelta(days=2)).isoformat()
        stamp = old + 'T15:00:00Z'
        self.mock({'GDXU': [{'t': stamp, 'c': 101.0}]},
                  {'GDXU': [{'t': stamp, 'c': 99.0}],
                   'BIL': [{'t': stamp, 'c': 92.0}]})
        bars = self.data.history(['GDXU', 'BIL'], self.session)
        self.assertEqual(bars['GDXU'][old], 101.0)
        self.assertEqual(bars['BIL'][old], 92.0)
        self.assertEqual(self.data.last_history_sources['iex_filled_missing_bars'], 1)

    def test_historical_sip_403_falls_back_to_iex(self):
        old = (self.now.astimezone(NY).date() - dt.timedelta(days=2)).isoformat()
        stamp = old + 'T15:00:00Z'
        self.mock({}, {'GDXU': [{'t': stamp, 'c': 99.0}]}, sip_status=403)
        bars = self.data.history(['GDXU'], self.session)
        self.assertEqual(bars['GDXU'][old], 99.0)
        self.assertFalse(self.data.sip_history_allowed)
        self.assertIn('403', self.data.last_history_sources['sip'])


if __name__ == '__main__':
    unittest.main()
