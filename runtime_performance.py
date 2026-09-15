"""Publish the all-history return independently of optional research date filters."""
from pathlib import Path
from analytics import read_events
from config import ALPACA_PAPER, VIRTUAL_STARTING_CAPITAL
from bot_logger import bot_log
from cycle_performance import long_result, publish


def report_cycle():
    def calculate():
        from options_trader import trading_client
        events = read_events()
        from research import is_own_event
        events = [r for r in events if is_own_event(r)]
        positions = trading_client.get_all_positions()
        prices = {p.symbol: p.current_price for p in positions if p.current_price is not None}
        quantities = {p.symbol: p.qty for p in positions}
        return long_result(events, prices, VIRTUAL_STARTING_CAPITAL, quantities)
    publish(Path(__file__).resolve().parent, 'options_inverted', calculate, bot_log, ALPACA_PAPER)
