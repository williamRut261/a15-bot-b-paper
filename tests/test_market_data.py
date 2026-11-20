"""Offline safety tests for the optional quote-midpoint research fallback.

No network requests, credentials, or real broker orders.
"""
import datetime as dt
import unittest

from alpaca_market_data import AlpacaMarketData, NY


class MarketDataFallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        self.day = self.now.astimezone(NY).date().isoformat()

    def make(self, trade=None, quote=None, fallback=True):
        client = AlpacaMarketData(
            feed='iex', key='test-id', secret='test-secret',
            quote_fallback=fallback, max_quote_spread_pct=1.0,
            max_quote_age_s=120)
        client._get = lambda *args, **kwargs: {
            'snapshots': {'GDXU': {
                'latestTrade': trade or {}, 'latestQuote': quote or {}}}}
        return client.snapshot(['GDXU'], self.day)

    def test_fresh_quote_fills_missing_iex_trade(self):
        quote = {'bp': 99.8, 'ap': 100.2, 't': self.now.isoformat()}
        prices, ages, times = self.make(quote=quote)
        self.assertAlmostEqual(prices['GDXU'], 100.0)
        self.assertIn('GDXU', ages)
        self.assertIn('GDXU', times)

    def test_quote_fallback_must_be_explicit(self):
        quote = {'bp': 99.8, 'ap': 100.2, 't': self.now.isoformat()}
        prices, _, _ = self.make(quote=quote, fallback=False)
        self.assertNotIn('GDXU', prices)

    def test_fresh_trade_takes_priority_over_quote(self):
        trade = {'p': 98.0, 't': self.now.isoformat()}
        quote = {'bp': 99.8, 'ap': 100.2, 't': self.now.isoformat()}
        prices, _, _ = self.make(trade=trade, quote=quote)
        self.assertEqual(prices['GDXU'], 98.0)

    def test_fresh_quote_replaces_stale_trade(self):
        trade = {'p': 98.0, 't': (self.now - dt.timedelta(minutes=3)).isoformat()}
        quote = {'bp': 99.8, 'ap': 100.2, 't': self.now.isoformat()}
        prices, _, _ = self.make(trade=trade, quote=quote)
        self.assertEqual(prices['GDXU'], 100.0)

    def test_reject_crossed_and_excessive_spread_quotes(self):
        for bp, ap in [(100, 99), (90, 110), (0, 100)]:
            quote = {'bp': bp, 'ap': ap, 't': self.now.isoformat()}
            prices, _, _ = self.make(quote=quote)
            self.assertNotIn('GDXU', prices)

    def test_reject_yesterdays_quote(self):
        quote = {'bp': 99.8, 'ap': 100.2,
                 't': (self.now - dt.timedelta(days=1)).isoformat()}
        prices, _, _ = self.make(quote=quote)
        self.assertNotIn('GDXU', prices)

    def test_reject_quote_too_old(self):
        quote = {'bp': 99.8, 'ap': 100.2,
                 't': (self.now - dt.timedelta(minutes=10)).isoformat()}
        prices, _, _ = self.make(quote=quote)
        self.assertNotIn('GDXU', prices)


if __name__ == '__main__':
    unittest.main()
