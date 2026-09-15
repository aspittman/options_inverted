from cycle_performance import completed_cycle
from runtime_performance import report_cycle
import time
import process_lock as fcntl
from pathlib import Path
from config import LOG_FILE, UNIVERSE_PROFILE
from research import qualified_opportunity, update_opportunity, capital_snapshot

from config import (
    UNDERLYINGS,
    MA_SHORT,
    MA_LONG,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    MIN_DTE,
    MAX_DTE,
    ENABLE_MARKET_REGIME_FILTER,
    MARKET_REGIME_SYMBOL,
    MARKET_REGIME_SHORT_MA,
    MARKET_REGIME_LONG_MA,
    OPTION_TYPE,
    CONTRACT_QTY,
    MAX_POSITIONS,
    MAX_POSITIONS_PER_CORRELATION_GROUP,
    correlation_group,
    ENABLE_NEW_ENTRIES,
    PAPER_STRATEGIES,
    REENTRY_COOLDOWN_DAYS,
    UNDERLYING_TRAILING_STOP_PERCENT,
    UNDERLYING_TAKE_PROFIT_PCT,
    SCAN_INTERVAL_SECONDS
)

from oasis import refresh_oasis_data, get_oasis_signal_state, entry_window
from analytics import (
    cooldown_active,
    get_strategy_open_lots,
    get_submitted_orders,
    record_event,
    signal_bar_already_submitted,
)
from bot_logger import bot_log, setup_logging
from strategy import (
    configure_daily_data_client,
    is_bearish_setup,
    get_bearish_signal_state,
    is_market_regime_bearish,
    is_underlying_exit_signal,
    latest_completed_bar_date,
    wait_for_market_open
)

from options_trader import (
    stock_data_client,
    trading_client,
    has_earnings_soon,
    get_option_contract,
    manage_underlying_exits,
    buy_option_contract,
    log_open_option_positions,
    log_analytics_summary,
    log_account_info,
    reconcile_order_fills,
    cancel_blocked_entry_orders,
    bootstrap_legacy_positions,
    reconcile_strategy_lots_with_broker,
)


def _run_bot():
    setup_logging()
    configure_daily_data_client(stock_data_client)
    wait_for_market_open(trading_client)

    bot_log("Starting options paper trading bot...")
    bot_log(f"Universe profile={UNIVERSE_PROFILE} | symbols={len(UNDERLYINGS)}")
    if not ENABLE_NEW_ENTRIES:
        bot_log("New entries are disabled; existing positions will still be managed.")
    last_entry_bar_date = None

    while True:
        with completed_cycle(report_cycle, lambda: time.sleep(SCAN_INTERVAL_SECONDS)):
            # Re-check every cycle so stale overnight/weekend quotes are not used.
            wait_for_market_open(trading_client)
            reconcile_order_fills()
            market_clock = trading_client.get_clock()
            cancel_blocked_entry_orders(market_clock)
            reconcile_order_fills()
            refresh_oasis_data(UNDERLYINGS, stock_data_client, trading_client, market_clock.timestamp)
            bootstrap_legacy_positions()
            reconcile_strategy_lots_with_broker()
            bot_positions = log_open_option_positions()
            log_account_info(bot_positions)
            log_analytics_summary()
            manage_underlying_exits(
                UNDERLYINGS,
                lambda symbol, signal: is_underlying_exit_signal(
                    symbol,
                    MA_SHORT,
                    MA_LONG,
                    MACD_FAST,
                    MACD_SLOW,
                    MACD_SIGNAL,
                    signal=signal,
                ),
                UNDERLYING_TRAILING_STOP_PERCENT,
                UNDERLYING_TAKE_PROFIT_PCT
            )

            if not ENABLE_NEW_ENTRIES:
                record_event("SKIP", reason="new_entries_disabled")
                bot_log("CYCLE SUMMARY | exits monitored | entry scan disabled")
                continue

            entry_bar_date = latest_completed_bar_date(MARKET_REGIME_SYMBOL)
            daily_due = entry_bar_date is not None and entry_bar_date != last_entry_bar_date
            if daily_due:
                last_entry_bar_date = entry_bar_date
            active_variants = [v for v in PAPER_STRATEGIES
                               if (entry_window(market_clock) if v.get("intraday") else daily_due)]

            checked = 0
            new_signals = 0
            blocked = 0
            orders_submitted = 0

            market_regime_ok = True
            if ENABLE_MARKET_REGIME_FILTER:
                market_regime_ok = is_market_regime_bearish(
                    MARKET_REGIME_SYMBOL,
                    MARKET_REGIME_SHORT_MA,
                    MARKET_REGIME_LONG_MA
                )

                if not market_regime_ok:
                    bot_log("Market regime is not bearish. Skipping new entries this cycle.")
                    record_event(
                        "SKIP",
                        underlying=MARKET_REGIME_SYMBOL,
                        reason="market_regime_not_bearish"
                    )

            for underlying in UNDERLYINGS:
                bot_log(f"=== Checking {underlying} ===")
                checked += 1

                if not market_regime_ok:
                    continue

                for variant in active_variants:
                    name = variant["name"]
                    state = get_oasis_signal_state(underlying) if variant.get("intraday") else get_bearish_signal_state(
                        underlying, MA_SHORT, MA_LONG, MACD_FAST, MACD_SLOW,
                        MACD_SIGNAL, signal=variant["signal"])
                    if not state.get("data_available", True) or not state.get("bearish", state.get("bullish", False)) or not state["new_signal"]:
                        continue
                    # Only genuine fresh signals enter the research opportunity log.
                    # Guards are applied afterward, so their rejections remain data.
                    new_signals += 1
                    with qualified_opportunity(name, underlying, state["signal_date"],
                                               "bearish" if ENABLE_MARKET_REGIME_FILTER else "filter_disabled"):
                        update_opportunity(virtual_capital_available=capital_snapshot()["available"])
                        record_event("SIGNAL_QUALIFIED", strategy=name, underlying=underlying,
                                     details=f"signal_date={state['signal_date']};market_regime=bearish")
                        if signal_bar_already_submitted(name, underlying, state["signal_date"]):
                            record_event("SKIP", strategy=name, underlying=underlying,
                                         reason="signal_bar_already_traded")
                            blocked += 1
                            continue
                        if not variant.get("intraday") and cooldown_active(name, underlying, REENTRY_COOLDOWN_DAYS):
                            record_event("SKIP", strategy=name, underlying=underlying,
                                         reason="reentry_cooldown")
                            blocked += 1
                            continue
                        if has_earnings_soon(underlying):
                            # The earnings helper already records the detailed skip.
                            blocked += 1
                            continue
                        option_symbol = get_option_contract(underlying, option_type=OPTION_TYPE,
                                                            min_dte=MIN_DTE, max_dte=MAX_DTE)
                        if not option_symbol:
                            blocked += 1
                            continue
                        submitted = buy_option_contract(option_symbol, qty=CONTRACT_QTY,
                            underlying=underlying, strategy=name,
                            max_entry_premium=variant["max_premium"], signal_date=state["signal_date"])
                        if submitted: orders_submitted += 1
                        else: blocked += 1


            bot_log(
                f"CYCLE SUMMARY | daily bar={entry_bar_date} | symbols checked={checked} | "
                f"new signals={new_signals} | blocked={blocked} | "
                f"orders submitted={orders_submitted}"
            )


def run_bot():
    # A local process lock prevents two instances spending the same reservations.
    lock_path = Path(LOG_FILE).parent / "long_put.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another LongPutBot instance is already running")
        _run_bot()


if __name__ == "__main__":
    run_bot()
