import time

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
    bootstrap_legacy_positions,
    reconcile_strategy_lots_with_broker,
)


def run_bot():
    setup_logging()
    configure_daily_data_client(stock_data_client)
    wait_for_market_open(trading_client)

    bot_log("Starting options paper trading bot...")
    if not ENABLE_NEW_ENTRIES:
        bot_log("New entries are disabled; existing positions will still be managed.")
    last_entry_bar_date = None

    while True:
        # Re-check every cycle so stale overnight/weekend quotes are not used.
        wait_for_market_open(trading_client)
        reconcile_order_fills()
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
            time.sleep(SCAN_INTERVAL_SECONDS)
            continue

        entry_bar_date = latest_completed_bar_date(MARKET_REGIME_SYMBOL)
        if entry_bar_date is None:
            bot_log("CYCLE SUMMARY | exits monitored | entry scan unavailable: no daily bars")
            time.sleep(SCAN_INTERVAL_SECONDS)
            continue
        if entry_bar_date == last_entry_bar_date:
            bot_log(
                f"CYCLE SUMMARY | exits monitored | entry scan not due "
                f"(last completed bar {entry_bar_date})"
            )
            time.sleep(SCAN_INTERVAL_SECONDS)
            continue
        last_entry_bar_date = entry_bar_date

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

            if has_earnings_soon(underlying):
                blocked += 1
                continue

            eligible_variants = []
            for variant in PAPER_STRATEGIES:
                state = get_bearish_signal_state(
                    underlying,
                    MA_SHORT,
                    MA_LONG,
                    MACD_FAST,
                    MACD_SLOW,
                    MACD_SIGNAL,
                    signal=variant["signal"],
                )
                if not state.get("data_available", True):
                    record_event(
                        "SKIP", strategy=variant["name"], underlying=underlying,
                        reason="signal_data_unavailable"
                    )
                elif not state["bearish"]:
                    record_event(
                        "SKIP", strategy=variant["name"], underlying=underlying,
                        reason=f"not_bearish_{variant['signal']}"
                    )
                elif not state["new_signal"]:
                    record_event(
                        "SKIP", strategy=variant["name"], underlying=underlying,
                        reason="signal_not_new", details=f"signal_date={state['signal_date']}"
                    )
                elif signal_bar_already_submitted(
                    variant["name"], underlying, state["signal_date"]
                ):
                    record_event(
                        "SKIP", strategy=variant["name"], underlying=underlying,
                        reason="signal_bar_already_traded",
                        details=f"signal_date={state['signal_date']}"
                    )
                elif cooldown_active(
                    variant["name"], underlying, REENTRY_COOLDOWN_DAYS
                ):
                    record_event(
                        "SKIP", strategy=variant["name"], underlying=underlying,
                        reason="reentry_cooldown",
                        details=f"cooldown_trading_days={REENTRY_COOLDOWN_DAYS}"
                    )
                else:
                    eligible_variants.append((variant, state["signal_date"]))

            new_signals += len(eligible_variants)

            if not eligible_variants:
                bot_log(f"No daily bearish setup for {underlying}.")
                continue

            option_symbol = get_option_contract(
                underlying,
                option_type=OPTION_TYPE,
                min_dte=MIN_DTE,
                max_dte=MAX_DTE
            )

            if option_symbol:
                lots = get_strategy_open_lots()
                pending = get_submitted_orders().values()
                reserved_count = len(lots) + sum(
                    1 for row in pending if row.get("order_side") == "buy"
                )
                reserved_group_counts = {}
                for _, lot_underlying, _ in lots:
                    group = correlation_group(lot_underlying)
                    reserved_group_counts[group] = reserved_group_counts.get(group, 0) + 1
                for row in pending:
                    if row.get("order_side") == "buy":
                        group = correlation_group(row.get("underlying", ""))
                        reserved_group_counts[group] = reserved_group_counts.get(group, 0) + 1
                for variant, signal_date in eligible_variants:
                    strategy_name = variant["name"]
                    if reserved_count >= MAX_POSITIONS:
                        bot_log(
                            f"Global position limit reached: strategy={strategy_name} "
                            f"MAX_POSITIONS={MAX_POSITIONS}"
                        )
                        record_event(
                            "SKIP", strategy=strategy_name, underlying=underlying,
                            reason="max_positions"
                        )
                        blocked += 1
                        continue
                    target_group = correlation_group(underlying)
                    if reserved_group_counts.get(target_group, 0) >= MAX_POSITIONS_PER_CORRELATION_GROUP:
                        bot_log(
                            f"Correlation-group limit reached: group={target_group} "
                            f"underlying={underlying}"
                        )
                        record_event(
                            "SKIP", strategy=strategy_name, underlying=underlying,
                            reason=f"correlation_group_{target_group}"
                        )
                        blocked += 1
                        continue
                    already_holds = any(
                        strategy == strategy_name and lot_underlying == underlying
                        for strategy, lot_underlying, _ in lots
                    )
                    if already_holds:
                        record_event(
                            "SKIP", strategy=strategy_name, underlying=underlying,
                            reason="already_holding"
                        )
                        blocked += 1
                        continue
                    submitted = buy_option_contract(
                        option_symbol,
                        qty=CONTRACT_QTY,
                        underlying=underlying,
                        strategy=strategy_name,
                        max_entry_premium=variant["max_premium"],
                        signal_date=signal_date,
                    )
                    if submitted:
                        orders_submitted += 1
                        reserved_count += 1
                        reserved_group_counts[target_group] = (
                            reserved_group_counts.get(target_group, 0) + 1
                        )
                    else:
                        blocked += 1
            else:
                blocked += len(eligible_variants)

            time.sleep(2)

        bot_log(
            f"CYCLE SUMMARY | daily bar={entry_bar_date} | symbols checked={checked} | "
            f"new signals={new_signals} | blocked={blocked} | "
            f"orders submitted={orders_submitted}"
        )
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_bot()
