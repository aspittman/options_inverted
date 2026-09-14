import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import options_trader

class OasisExecutionTests(unittest.TestCase):
    def run_exit(self, name="oasis", price=4, minutes_left=120, opened_at="2026-09-14T10:00:00"):
        symbol = "ABC261218P00100000"
        now = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
        clock = SimpleNamespace(timestamp=now, next_close=now + timedelta(minutes=minutes_left), is_open=True)
        lot = dict(qty=1, cost=4, underlying_cost=100, opened_at=opened_at)
        with patch.object(options_trader, "get_options_inverted_positions", return_value=[SimpleNamespace(symbol=symbol, current_price=price)]), \
             patch.object(options_trader, "get_strategy_open_lots", return_value={(name, "ABC", symbol): lot}), \
             patch.object(options_trader, "get_underlying_low_water_marks", return_value={}), \
             patch.object(options_trader, "get_underlying_price", return_value=100), \
             patch.object(options_trader.trading_client, "get_clock", return_value=clock), \
             patch.object(options_trader, "record_event"), \
             patch.object(options_trader, "close_strategy_lot") as close:
            options_trader.manage_underlying_exits(["ABC"], lambda *args: (False, ""), .03, .08)
            return close.call_args

    def test_oasis_twenty_percent_stop_and_regular_original_thirty_percent_stop(self):
        self.assertIn("option_stop_loss", self.run_exit(price=3.2).args[-1])
        self.assertIsNone(self.run_exit(price=3.21))
        self.assertIsNone(self.run_exit(name="regular", price=3.2))
        self.assertIn("option_stop_loss", self.run_exit(name="regular", price=2.8).args[-1])

    def test_early_close_and_overnight_recovery_only_close_oasis(self):
        self.assertEqual(self.run_exit(minutes_left=15).args[-1], "oasis_session_close")
        self.assertIsNone(self.run_exit(name="regular", minutes_left=15))
        self.assertEqual(self.run_exit(opened_at="2026-09-11T10:00:00").args[-1], "oasis_overnight_recovery")

    def test_cutoff_blocks_order_submission_at_broker_reported_early_close(self):
        now = datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)
        clock = SimpleNamespace(timestamp=now, next_close=now + timedelta(minutes=30), is_open=True)
        with patch.object(options_trader, "loss_reentry_block_active", return_value=False), \
             patch.object(options_trader.trading_client, "get_clock", return_value=clock), \
             patch.object(options_trader, "get_options_inverted_positions") as broker, \
             patch.object(options_trader, "record_event"):
            self.assertFalse(options_trader.buy_option_contract("ABC261218P00100000", strategy="oasis"))
            broker.assert_not_called()

