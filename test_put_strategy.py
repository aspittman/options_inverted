"""Offline regression tests for the directional long-put conversion."""
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import strategy
import backtester
import options_trader
from alpaca.trading.enums import ContractType
from alpaca_option_backtest import AlpacaOptionRepricer


def trend_indicators():
    return {key: pd.Series(values) for key, values in {
        'ma_short': [102., 101.], 'ma_long': [110., 110.],
        'macd': [-2., -2.], 'macd_signal': [-1., -1.],
        'macd_hist': [-1., -1.],
    }.items()}


class PutStrategyTests(unittest.TestCase):
    def test_live_and_backtest_agree_on_bearish_trend(self):
        close = pd.Series([100., 99.])
        indicators = trend_indicators()
        self.assertTrue(strategy._bearish_at(close, indicators, 1, 'daily_trend'))
        self.assertTrue(backtester.is_bearish_at(close, indicators, 1))
        indicators['ma_short'] = pd.Series([100., 101.])
        self.assertFalse(strategy._bearish_at(close, indicators, 1, 'daily_trend'))
        self.assertFalse(backtester.is_bearish_at(close, indicators, 1))

    def test_bullish_trend_is_rejected(self):
        close = pd.Series([120., 121.])
        indicators = trend_indicators()
        self.assertFalse(strategy._bearish_at(close, indicators, 1, 'daily_trend'))
        self.assertFalse(backtester.is_bearish_at(close, indicators, 1))

    def test_market_filter_accepts_decline_rejects_rally(self):
        for values, expected in [(range(400, 100, -1), True), (range(100, 400), False)]:
            with patch('strategy._completed_daily_close', return_value=pd.Series(values, dtype=float)):
                self.assertEqual(strategy.is_market_regime_bearish('SPY', 50, 200), expected)

    def test_swing_requires_rejection_and_mirrored_rsi_range(self):
        indicators = trend_indicators()
        indicators.update({key: pd.Series(values) for key, values in {
            'ema_10': [102., 102.], 'ema_20': [102., 102.],
            'rsi': [50., 40.], 'ma_50': [102., 101.], 'ma_200': [110., 110.],
        }.items()})
        close = pd.Series([103., 100.])
        self.assertTrue(strategy._bearish_at(close, indicators, 1, 'daily_swing'))
        self.assertTrue(backtester.is_swing_entry_at(close, indicators, 1))
        indicators['rsi'] = pd.Series([50., 60.])
        self.assertFalse(strategy._bearish_at(close, indicators, 1, 'daily_swing'))
        self.assertFalse(backtester.is_swing_entry_at(close, indicators, 1))

    def test_reversal_exits_agree(self):
        close = pd.Series([100.] * 205)
        indicators = {key: pd.Series([float(value.iloc[-1])] * 205)
                      for key, value in trend_indicators().items()}
        indicators['macd_hist'].iloc[-1] = 0.2
        with patch('strategy._completed_daily_close', return_value=close), patch('strategy._daily_indicators', return_value=indicators):
            self.assertEqual(strategy.is_underlying_exit_signal('SPY', 50, 200, 12, 26, 9), (True, 'macd_hist_positive'))
        self.assertEqual(backtester.bullish_exit_reason(close, indicators, 204), 'macd_hist_positive')

    def test_put_delta_strike_and_profit_direction(self):
        strike = backtester.estimate_strike_for_delta(100, -0.6, 75)
        self.assertGreater(strike, 100)
        self.assertAlmostEqual(backtester.estimate_put_delta(100, strike, 75), -0.6, places=3)
        up, _ = backtester.estimate_option_price(110, strike, 75)
        entry, _ = backtester.estimate_option_price(100, strike, 75)
        down, delta = backtester.estimate_option_price(90, strike, 75)
        self.assertGreater(down, entry)
        self.assertGreater(entry, up)
        self.assertLess(delta, -0.6)
        expiry, _ = backtester.estimate_option_price(90, 105, 0)
        self.assertEqual(expiry, 15)

    def test_contract_score_accepts_negative_delta_rejects_call_delta(self):
        contract = SimpleNamespace(expiration_date=date.today() + timedelta(days=75), strike_price=101)
        snapshot = SimpleNamespace(greeks=SimpleNamespace(delta=-0.6), latest_quote=SimpleNamespace(bid_price=2, ask_price=2.04))
        self.assertIsNotNone(options_trader.contract_score(contract, snapshot, 200, 100))
        snapshot.greeks.delta = 0.6
        self.assertIsNone(options_trader.contract_score(contract, snapshot, 200, 100))

    def test_buy_rejects_calls_before_broker_access(self):
        with patch.object(options_trader.trading_client, 'submit_order') as submit:
            with self.assertRaises(ValueError):
                options_trader.buy_option_contract('SPY261218C00500000')
            submit.assert_not_called()

    def test_historical_contract_requests_are_puts(self):
        repricer = AlpacaOptionRepricer()
        with patch.object(repricer.trading, 'get_option_contracts', return_value=SimpleNamespace(option_contracts=[], next_page_token=None)) as contracts:
            repricer._contracts('SPY', '2026-01-01', 101)
        self.assertEqual(contracts.call_count, 2)
        for request in contracts.call_args_list:
            self.assertEqual(request.args[0].type, ContractType.PUT)


if __name__ == '__main__':
    unittest.main()
