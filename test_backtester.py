import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import options_trader
import strategy

from backtester import apply_portfolio_constraints, build_swing_signals, is_swing_entry_at
from config import correlation_group
from analytics import (
    cooldown_active,
    get_underlying_low_water_marks,
    signal_bar_already_submitted,
    summarize_performance_since,
)
from options_trader import (
    calculate_underlying_trailing_stop,
    close_strategy_lot,
    has_earnings_soon,
)


def trade(symbol, entry_date, exit_date, entry_price, exit_price):
    return {
        "symbol": symbol,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "estimated_option_entry_price": entry_price,
        "estimated_option_exit_price": exit_price,
    }


class TerminalSummaryTests(unittest.TestCase):
    def test_performance_row_shows_trade_cap_and_signed_gain_loss(self):
        result = {
            "deployed_premium": 500,
            "realized_pnl": 75,
            "unrealized_pnl": -25,
            "total_pnl": 50,
            "return_pct": 10,
        }

        row = options_trader._performance_table_row("regular", 500, result)

        self.assertIn("regular", row)
        self.assertIn("$500", row)
        self.assertIn("$     50.00", row)
        self.assertIn("+10.00%", row)


class PortfolioConstraintTests(unittest.TestCase):
    def test_allows_two_concurrent_positions_and_rejects_third(self):
        candidates = [
            trade("A", "2026-01-01", "2026-01-03", 0.75, 1.00),
            trade("B", "2026-01-01", "2026-01-02", 0.75, 0.50),
            trade("C", "2026-01-01", "2026-01-02", 0.25, 0.50),
        ]

        selected = apply_portfolio_constraints(
            candidates,
            starting_cash=2500,
            max_positions=2,
            max_total_premium=200,
            max_entry_premium=100,
        )

        self.assertEqual([item["symbol"] for item in selected], ["A", "B"])

    def test_releases_cash_after_exit_for_a_later_entry(self):
        candidates = [
            trade("A", "2026-01-01", "2026-01-02", 1.00, 0.50),
            trade("B", "2026-01-03", "2026-01-04", 0.50, 0.75),
        ]

        selected = apply_portfolio_constraints(
            candidates,
            starting_cash=100,
            max_positions=2,
            max_total_premium=200,
            max_entry_premium=100,
        )

        self.assertEqual([item["symbol"] for item in selected], ["A", "B"])

    def test_enforces_per_trade_and_total_premium_limits(self):
        candidates = [
            trade("A", "2026-01-01", "2026-01-03", 1.01, 1.25),
            trade("B", "2026-01-01", "2026-01-03", 0.75, 1.00),
            trade("C", "2026-01-01", "2026-01-03", 0.50, 0.75),
        ]

        selected = apply_portfolio_constraints(
            candidates,
            starting_cash=2500,
            max_positions=2,
            max_total_premium=100,
            max_entry_premium=100,
        )

        self.assertEqual([item["symbol"] for item in selected], ["B"])

    def test_rejects_a_second_correlated_position(self):
        candidates = [
            trade("SPY", "2026-01-01", "2026-01-03", 0.50, 0.75),
            trade("QQQ", "2026-01-01", "2026-01-03", 0.50, 0.75),
        ]

        selected = apply_portfolio_constraints(
            candidates,
            starting_cash=2500,
            max_positions=2,
            max_total_premium=200,
            max_entry_premium=100,
        )

        self.assertEqual([item["symbol"] for item in selected], ["QQQ"])


class SwingSignalTests(unittest.TestCase):
    def test_requires_a_rally_rejection_in_a_bearish_regime(self):
        close = pd.Series([103.0, 101.0])
        indicators = {
            "ema_10": pd.Series([102.0, 102.5]),
            "ema_20": pd.Series([102.0, 102.0]),
            "ma_50": pd.Series([104.0, 104.0]),
            "ma_200": pd.Series([110.0, 110.0]),
            "rsi": pd.Series([50.0, 55.0]),
            "macd_hist": pd.Series([-0.1, -0.2]),
        }

        self.assertTrue(is_swing_entry_at(close, indicators, len(close) - 1))

    def test_rejects_a_price_below_the_long_term_average(self):
        close = pd.Series(
            list(range(300, 100, -1)) + [102, 103],
            index=pd.date_range("2025-01-01", periods=202),
            dtype=float,
        )
        indicators = build_swing_signals(close)

        self.assertFalse(is_swing_entry_at(close, indicators, len(close) - 1))


class CorrelationGroupTests(unittest.TestCase):
    def test_related_symbols_share_a_group(self):
        self.assertEqual(correlation_group("SPY"), correlation_group("QQQ"))
        self.assertEqual(correlation_group("XOM"), correlation_group("CVX"))

    def test_unlisted_symbol_gets_its_own_group(self):
        self.assertEqual(correlation_group("OTHER"), "OTHER")


class EntryGuardTests(unittest.TestCase):
    def setUp(self):
        options_trader._earnings_cache.clear()

    @patch("options_trader.yf.Ticker")
    def test_etf_does_not_request_earnings(self, ticker):
        self.assertFalse(has_earnings_soon("SPY"))
        ticker.assert_not_called()

    @patch("options_trader.yf.Ticker")
    def test_earnings_result_is_cached_for_the_day(self, ticker):
        ticker.return_value.get_earnings_dates.return_value = pd.DataFrame()

        self.assertFalse(has_earnings_soon("AAPL"))
        self.assertFalse(has_earnings_soon("AAPL"))
        ticker.assert_called_once_with("AAPL")

    @patch("options_trader.record_event")
    @patch("options_trader.yf.Ticker")
    def test_earnings_check_failure_blocks_entry(self, ticker, record_event):
        ticker.return_value.get_earnings_dates.side_effect = ImportError("missing parser")

        self.assertTrue(has_earnings_soon("AAPL"))
        record_event.assert_called_once_with(
            "SKIP",
            underlying="AAPL",
            reason="earnings_check_failed",
            details="error=ImportError",
        )


class AlpacaHistoryTests(unittest.TestCase):
    def setUp(self):
        strategy._daily_close_cache.clear()

    @patch("strategy.sleep")
    def test_daily_history_retries_after_empty_response(self, sleep):
        empty = MagicMock(data={"AAPL": []})
        bar = MagicMock(close=100.0, timestamp=pd.Timestamp("2026-08-31", tz="UTC"))
        populated = MagicMock(data={"AAPL": [bar]})
        client = MagicMock()
        client.get_stock_bars.side_effect = [empty, populated]
        strategy.configure_daily_data_client(client)

        result = strategy._download_daily_history("AAPL")

        self.assertEqual(result["Close"].tolist(), [100.0])
        self.assertEqual(client.get_stock_bars.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("strategy._download_daily_history", return_value=None)
    def test_failed_refresh_uses_last_successful_close(self, download):
        expected = pd.Series([100.0])
        strategy._daily_close_cache["AAPL"] = (strategy.monotonic() - 300, expected)

        self.assertIs(strategy._completed_daily_close("AAPL"), expected)

    @patch("strategy._download_daily_history", return_value=None)
    def test_refresh_does_not_use_overly_stale_close(self, download):
        strategy._daily_close_cache["AAPL"] = (
            strategy.monotonic() - strategy._DAILY_CLOSE_MAX_STALE_SECONDS - 1,
            pd.Series([100.0]),
        )

        self.assertIsNone(strategy._completed_daily_close("AAPL"))


class AnalyticsGuardTests(unittest.TestCase):
    @patch("analytics.read_events")
    def test_performance_summary_uses_only_fills_on_or_after_cutoff(self, read_events):
        read_events.return_value = [
            {
                "timestamp": "2026-08-20T12:00:00", "event": "ORDER_FILL",
                "strategy": "regular", "underlying": "OLD", "option_symbol": "OLD1",
                "qty": "1", "price": "9", "order_side": "buy",
            },
            {
                "timestamp": "2026-08-21T12:00:00", "event": "ORDER_FILL",
                "strategy": "regular", "underlying": "AAPL", "option_symbol": "AAPL1",
                "qty": "1", "price": "2", "order_side": "buy",
            },
            {
                "timestamp": "2026-08-22T12:00:00", "event": "ORDER_FILL",
                "strategy": "regular", "underlying": "MSFT", "option_symbol": "MSFT1",
                "qty": "1", "price": "3", "order_side": "buy",
            },
            {
                "timestamp": "2026-08-23T12:00:00", "event": "ORDER_FILL",
                "strategy": "regular", "underlying": "MSFT", "option_symbol": "MSFT1",
                "qty": "1", "price": "4", "order_side": "sell",
            },
        ]

        result = summarize_performance_since(
            "2026-08-21", current_prices={"AAPL1": 2.5}
        )

        self.assertEqual(result["deployed_premium"], 500)
        self.assertEqual(result["realized_pnl"], 100)
        self.assertEqual(result["unrealized_pnl"], 50)
        self.assertEqual(result["total_pnl"], 150)
        self.assertEqual(result["return_pct"], 0.6)
        self.assertEqual(result["return_on_capital_employed_percent"], 30)
        self.assertEqual(result["open_positions"], 1)
        self.assertEqual(result["positions_value"], 250)

        regular = summarize_performance_since(
            "2026-08-21", current_prices={"AAPL1": 2.5}, strategy="regular"
        )
        self.assertEqual(regular["deployed_premium"], 500)
        self.assertEqual(regular["return_pct"], 0.6)

    @patch("analytics.read_events")
    def test_cooldown_counts_trading_days(self, read_events):
        read_events.return_value = [{
            "timestamp": "2026-08-07T12:00:00", "event": "ORDER_FILL",
            "order_side": "sell", "strategy": "regular", "underlying": "SPY",
        }]
        self.assertTrue(cooldown_active(
            "regular", "SPY", 5, today=date(2026, 8, 12)
        ))
        self.assertFalse(cooldown_active(
            "regular", "SPY", 3, today=date(2026, 8, 12)
        ))

    @patch("analytics.read_events")
    def test_signal_bar_can_only_be_submitted_once(self, read_events):
        read_events.return_value = [{
            "event": "ORDER_SUBMITTED", "order_side": "buy",
            "strategy": "regular", "underlying": "SPY",
            "details": "signal_date=2026-08-11;limit_price=5.00",
        }]
        self.assertTrue(signal_bar_already_submitted("regular", "SPY", "2026-08-11"))
        self.assertFalse(signal_bar_already_submitted("regular", "SPY", "2026-08-12"))


class TrailingStopTests(unittest.TestCase):
    def test_stop_falls_with_stock_and_never_moves_back_up(self):
        high, stop = calculate_underlying_trailing_stop(100, 95, 97, 0.03)
        self.assertEqual(high, 95)
        self.assertAlmostEqual(stop, 97.85)

        lower_high, lower_stop = calculate_underlying_trailing_stop(
            100, 98, high, 0.03
        )
        self.assertEqual(lower_high, 95)
        self.assertAlmostEqual(lower_stop, 97.85)

    @patch("analytics.read_events")
    def test_low_water_is_rebuilt_after_restart(self, read_events):
        read_events.return_value = [
            {
                "event": "ORDER_FILL", "order_side": "buy", "qty": "1",
                "strategy": "regular", "underlying": "BAC",
                "option_symbol": "BAC261016P00062500", "underlying_price": "64",
            },
            {
                "event": "RISK_SNAPSHOT", "strategy": "regular",
                "underlying": "BAC", "option_symbol": "BAC261016P00062500",
                "underlying_price": "61",
            },
            {
                "event": "RISK_SNAPSHOT", "strategy": "regular",
                "underlying": "BAC", "option_symbol": "BAC261016P00062500",
                "underlying_price": "63",
            },
        ]

        marks = get_underlying_low_water_marks()
        self.assertEqual(marks[("regular", "BAC", "BAC261016P00062500")], 61)

    @patch("analytics.read_events")
    def test_closed_lot_does_not_leak_low_water_into_reentry(self, read_events):
        common = {
            "strategy": "regular", "underlying": "BAC",
            "option_symbol": "BAC261016P00062500",
        }
        read_events.return_value = [
            {**common, "event": "ORDER_FILL", "order_side": "buy", "qty": "1",
             "underlying_price": "64"},
            {**common, "event": "RISK_SNAPSHOT", "underlying_price": "70"},
            {**common, "event": "ORDER_FILL", "order_side": "sell", "qty": "1",
             "underlying_price": "68"},
            {**common, "event": "ORDER_FILL", "order_side": "buy", "qty": "1",
             "underlying_price": "60"},
        ]

        marks = get_underlying_low_water_marks()
        self.assertEqual(marks[("regular", "BAC", "BAC261016P00062500")], 60)

    @patch("options_trader.record_event")
    @patch("options_trader.get_underlying_price", return_value=64)
    @patch("options_trader.trading_client.submit_order")
    @patch("options_trader.get_option_snapshots")
    @patch("options_trader._strategy_has_pending_order", return_value=False)
    def test_risk_exit_uses_marketable_bid_limit(
        self, _pending, snapshots, submit_order, _price, _record_event
    ):
        quote = MagicMock(bid_price=3.10, ask_price=3.30)
        snapshots.return_value = {
            "BAC261016P00062500": MagicMock(latest_quote=quote)
        }
        submit_order.return_value = MagicMock(id="order-1", status="pending_new")

        # Exits now require both recorded ownership and available broker longs.
        with patch("options_trader.get_strategy_open_lots", return_value={
            ("regular", "BAC", "BAC261016P00062500"): {"qty": 1}
        }), patch.object(options_trader.trading_client, "get_all_positions", return_value=[
            MagicMock(symbol="BAC261016P00062500", qty="1", qty_available="1")
        ]), patch.object(options_trader.trading_client, "get_orders", return_value=[]), \
                patch("options_trader.get_submitted_orders", return_value={}):
            close_strategy_lot(
                "regular", "BAC", "BAC261016P00062500", 1,
                "underlying_trailing_stop_-3.00%",
            )

        order = submit_order.call_args.args[0]
        self.assertEqual(float(order.limit_price), 3.10)


if __name__ == "__main__":
    unittest.main()
