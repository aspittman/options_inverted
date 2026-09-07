import argparse
import csv
import math
from pathlib import Path

import ta
import yfinance as yf

from config import (
    BACKTEST_ENTRY_DTE,
    BACKTEST_OPTION_TIME_VALUE_PERCENT,
    BACKTEST_STARTING_CASH,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MA_LONG,
    MA_SHORT,
    MAX_HOLDING_DAYS,
    OPTION_STOP_LOSS_PERCENT,
    OPTION_TAKE_PROFIT_PERCENT,
    TARGET_DELTA,
    UNDERLYING_TRAILING_STOP_PERCENT,
    UNDERLYINGS,
    PAPER_STRATEGIES,
    MAX_POSITIONS,
    MAX_TOTAL_OPTION_PREMIUM,
    REGULAR_MAX_PREMIUM_PER_TRADE,
    MAX_100_PREMIUM_PER_TRADE,
    MAX_POSITIONS_PER_CORRELATION_GROUP,
    correlation_group,
)


RESULTS_FILE = Path("logs/options_backtest_trades.csv")
EQUITY_CURVE_FILE = Path("logs/options_backtest_equity_curve.csv")
CHEAP_RESULTS_FILE = Path("logs/options_backtest_trades_100_max.csv")
CHEAP_EQUITY_CURVE_FILE = Path("logs/options_backtest_equity_curve_100_max.csv")
CONTRACT_MULTIPLIER = 100
CHEAP_MAX_PREMIUM = MAX_100_PREMIUM_PER_TRADE
YEARS_TO_PERIOD = {1: "1y", 3: "3y", 5: "5y"}

FIELDNAMES = [
    "symbol",
    "entry_date",
    "exit_date",
    "entry_dte",
    "exit_dte",
    "estimated_strike",
    "estimated_entry_delta",
    "estimated_exit_delta",
    "entry_underlying_price",
    "exit_underlying_price",
    "estimated_option_entry_price",
    "estimated_option_exit_price",
    "pnl_dollars",
    "pnl_percent",
    "exit_reason",
]

EQUITY_FIELDNAMES = ["date", "trade_number", "pnl_dollars", "equity", "drawdown"]
SWING_MAX_HOLDING_DAYS = 15
SWING_STOP_LOSS_PERCENT = 0.03
SWING_TAKE_PROFIT_PERCENT = 0.06


def apply_portfolio_constraints(
    trades,
    starting_cash=BACKTEST_STARTING_CASH,
    max_positions=MAX_POSITIONS,
    max_total_premium=MAX_TOTAL_OPTION_PREMIUM,
    max_entry_premium=None,
    max_positions_per_group=MAX_POSITIONS_PER_CORRELATION_GROUP,
):
    """Select affordable trades chronologically and enforce portfolio exposure.

    Entry and exit prices are still estimates, but this prevents the backtest from
    spending cash it does not have or opening more positions than the live bot.
    Candidates with the same entry date are selected deterministically by symbol.
    """
    cash = float(starting_cash)
    active = []
    selected = []

    for trade in sorted(trades, key=lambda item: (item["entry_date"], item["symbol"])):
        entry_date = trade["entry_date"]
        still_active = []
        for position in active:
            if position["exit_date"] < entry_date:
                cash += float(position["estimated_option_exit_price"]) * CONTRACT_MULTIPLIER
            else:
                still_active.append(position)
        active = still_active

        premium = float(trade["estimated_option_entry_price"]) * CONTRACT_MULTIPLIER
        deployed = sum(
            float(position["estimated_option_entry_price"]) * CONTRACT_MULTIPLIER
            for position in active
        )
        if max_entry_premium is not None and premium > max_entry_premium:
            continue
        if len(active) >= max_positions:
            continue
        group_count = sum(
            1 for position in active
            if correlation_group(position["symbol"]) == correlation_group(trade["symbol"])
        )
        if group_count >= max_positions_per_group:
            continue
        if premium > cash or deployed + premium > max_total_premium:
            continue

        cash -= premium
        active.append(trade)
        selected.append(trade)

    return selected


def get_close_series(symbol, period, interval):
    data = yf.download(symbol, period=period, interval=interval, progress=False)

    if data is None or data.empty:
        return None

    close = data["Close"]
    if hasattr(close, "columns"):
        close = close.squeeze()

    close = close.dropna()
    return close if not close.empty else None


def build_signals(close):
    ma_short_series = close.rolling(MA_SHORT).mean()
    ma_long_series = close.rolling(MA_LONG).mean()

    macd = ta.trend.MACD(
        close=close,
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL,
    )

    indicators = {
        "ma_short": ma_short_series,
        "ma_long": ma_long_series,
        "macd": macd.macd(),
        "macd_signal": macd.macd_signal(),
        "macd_hist": macd.macd_diff(),
    }
    return indicators


def build_swing_signals(close):
    """Daily trend/pullback indicators for the experimental swing strategy."""
    macd = ta.trend.MACD(
        close=close,
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL,
    )
    return {
        "ema_10": close.ewm(span=10, adjust=False).mean(),
        "ema_20": close.ewm(span=20, adjust=False).mean(),
        "ma_50": close.rolling(50).mean(),
        "ma_200": close.rolling(200).mean(),
        "rsi": ta.momentum.RSIIndicator(close, window=14).rsi(),
        "macd_hist": macd.macd_diff(),
    }


def is_swing_entry_at(close, indicators, index):
    if index <= 0:
        return False
    values = [
        close.iloc[index],
        close.iloc[index - 1],
        indicators["ema_10"].iloc[index],
        indicators["ema_20"].iloc[index],
        indicators["ema_20"].iloc[index - 1],
        indicators["ma_50"].iloc[index],
        indicators["ma_200"].iloc[index],
        indicators["rsi"].iloc[index],
        indicators["macd_hist"].iloc[index],
    ]
    if any(value != value for value in values):
        return False

    latest, previous, ema_10, ema_20, previous_ema_20, ma_50, ma_200, rsi, macd_hist = values
    bearish_regime = latest < ma_200 and ma_50 < ma_200
    rally_rejected = previous >= previous_ema_20 and latest < ema_20
    confirmation = latest < ema_10 and 35 <= rsi <= 55 and macd_hist < 0
    return bearish_regime and rally_rejected and confirmation


def backtest_underlying_signal(symbol, close, strategy):
    """Evaluate signal expectancy without depending on synthetic option prices."""
    minimum_bars = 205
    if len(close) < minimum_bars:
        return []
    current_indicators = build_signals(close)
    swing_indicators = build_swing_signals(close)
    trades = []
    entry_index = None
    underlying_low = None

    for index in range(minimum_bars, len(close)):
        if entry_index is None:
            enters = (
                is_bearish_at(close, current_indicators, index)
                if strategy == "current"
                else is_swing_entry_at(close, swing_indicators, index)
            )
            if enters:
                entry_index = index
                underlying_low = float(close.iloc[index])
            continue

        entry_price = float(close.iloc[entry_index])
        exit_price = float(close.iloc[index])
        return_pct = (entry_price - exit_price) / entry_price
        underlying_low = min(underlying_low, exit_price)
        reason = ""
        if exit_price >= underlying_low * (1 + UNDERLYING_TRAILING_STOP_PERCENT):
            reason = "underlying_trailing_stop"
        elif strategy == "swing":
            if return_pct >= SWING_TAKE_PROFIT_PERCENT:
                reason = "underlying_take_profit"
            elif index - entry_index >= SWING_MAX_HOLDING_DAYS:
                reason = "max_holding_days"
            elif exit_price > float(swing_indicators["ema_20"].iloc[index]):
                reason = "close_above_ema_20"
        else:
            if index - entry_index >= MAX_HOLDING_DAYS:
                reason = "max_holding_days"
            else:
                reason = bullish_exit_reason(close, current_indicators, index)

        if reason:
            trades.append({
                "symbol": symbol,
                "entry_date": close.index[entry_index].date().isoformat(),
                "exit_date": close.index[index].date().isoformat(),
                "pnl_dollars": round(return_pct * 100, 2),
                "pnl_percent": round(return_pct * 100, 2),
                "entry_underlying_price": round(entry_price, 2),
                "exit_underlying_price": round(exit_price, 2),
                "exit_reason": reason,
            })
            entry_index = None
            underlying_low = None

    return trades


def run_signal_comparison(period, interval):
    results = {"current": [], "swing": []}
    for symbol in UNDERLYINGS:
        close = get_close_series(symbol, period, interval)
        if close is None:
            continue
        for strategy in results:
            results[strategy].extend(backtest_underlying_signal(symbol, close, strategy))

    print("\nSignal comparison (P/L per $100 of bearish underlying exposure)")
    print("=====================================================")
    print("This evaluates entries and exits, not option pricing or fills.")
    print_summary(results["current"], "Current MA/MACD Signal")
    print_summary(results["swing"], "Experimental Daily Rally Rejection Swing Signal")
    return results


def run_alpaca_option_backtest(period, interval, strategy, max_candidates):
    if interval != "1d":
        raise ValueError("Alpaca option validation requires --interval 1d")
    from alpaca_option_backtest import reprice_candidates

    candidates = []
    for symbol in UNDERLYINGS:
        close = get_close_series(symbol, period, interval)
        if close is not None:
            candidates.extend(backtest_underlying_signal(symbol, close, strategy))
    candidates.sort(key=lambda item: (item["entry_date"], item["symbol"]))
    trades = reprice_candidates(
        candidates,
        max_entry_premium=100.0,
        max_candidates=max_candidates,
    )
    trades = apply_portfolio_constraints(
        trades,
        max_entry_premium=100.0,
    )
    print_summary(trades, f"Alpaca Historical Options: {strategy}")
    return trades


def is_bearish_at(close, indicators, index):
    if index <= 0:
        return False

    latest_close = close.iloc[index]
    latest_ma_short = indicators["ma_short"].iloc[index]
    prev_ma_short = indicators["ma_short"].iloc[index - 1]
    latest_ma_long = indicators["ma_long"].iloc[index]
    latest_macd = indicators["macd"].iloc[index]
    latest_macd_signal = indicators["macd_signal"].iloc[index]
    latest_macd_hist = indicators["macd_hist"].iloc[index]

    values = [
        latest_close,
        latest_ma_short,
        prev_ma_short,
        latest_ma_long,
        latest_macd,
        latest_macd_signal,
        latest_macd_hist,
    ]
    if any(value != value for value in values):
        return False

    in_downtrend = latest_close < latest_ma_short < latest_ma_long
    ma_falling = latest_ma_short < prev_ma_short
    macd_confirmed = latest_macd < latest_macd_signal and latest_macd_hist < 0

    return in_downtrend and ma_falling and macd_confirmed


def bullish_exit_reason(close, indicators, index):
    latest_close = close.iloc[index]
    latest_ma_short = indicators["ma_short"].iloc[index]
    latest_ma_long = indicators["ma_long"].iloc[index]
    latest_macd_hist = indicators["macd_hist"].iloc[index]

    values = [latest_close, latest_ma_short, latest_ma_long, latest_macd_hist]
    if any(value != value for value in values):
        return ""

    if latest_close > latest_ma_long:
        return "close_above_long_ma"

    if latest_ma_short > latest_ma_long:
        return "short_ma_above_long_ma"

    if latest_macd_hist > 0:
        return "macd_hist_positive"

    return ""


def estimate_put_delta(underlying_price, strike, dte):
    if underlying_price <= 0 or strike <= 0:
        return 0.0

    years_to_expiration = max(dte, 1) / 365
    time_scale = max(math.sqrt(years_to_expiration), 0.05)
    moneyness = (underlying_price - strike) / underlying_price
    exponent = -moneyness * 12 / time_scale
    exponent = max(min(exponent, 60), -60)

    return 1 / (1 + math.exp(exponent)) - 1


def estimate_strike_for_delta(underlying_price, target_delta, dte):
    lower_strike = underlying_price * 0.5
    upper_strike = underlying_price * 1.5

    for _ in range(40):
        strike = (lower_strike + upper_strike) / 2
        delta = estimate_put_delta(underlying_price, strike, dte)

        if delta > target_delta:
            lower_strike = strike
        else:
            upper_strike = strike

    return round((lower_strike + upper_strike) / 2, 2)


def estimate_option_price(underlying_price, strike, dte):
    delta = estimate_put_delta(underlying_price, strike, dte)
    intrinsic_value = max(strike - underlying_price, 0)
    years_to_expiration = max(dte, 0) / 365
    time_value = (
        underlying_price
        * BACKTEST_OPTION_TIME_VALUE_PERCENT
        * math.sqrt(years_to_expiration)
        * max(0.25, 1 - abs(delta + 0.5))
    )

    return max(intrinsic_value + time_value, 0.01), delta


def holding_days_between(close, entry_index, exit_index):
    entry_date = close.index[entry_index]
    exit_date = close.index[exit_index]
    calendar_days = (exit_date - entry_date).days

    return max(calendar_days, exit_index - entry_index)


def option_mark_for_index(close, entry_index, current_index, strike):
    underlying_price = float(close.iloc[current_index])
    held_days = holding_days_between(close, entry_index, current_index)
    remaining_dte = max(BACKTEST_ENTRY_DTE - held_days, 0)
    option_price, delta = estimate_option_price(underlying_price, strike, remaining_dte)

    return option_price, delta, remaining_dte


def build_option_position(underlying_price):
    strike = estimate_strike_for_delta(underlying_price, TARGET_DELTA, BACKTEST_ENTRY_DTE)
    option_entry_price, entry_delta = estimate_option_price(
        underlying_price,
        strike,
        BACKTEST_ENTRY_DTE,
    )

    return {
        "strike": strike,
        "entry_price": option_entry_price,
        "entry_delta": entry_delta,
    }


def build_trade(symbol, close, entry_index, exit_index, exit_reason, option_position):
    entry_underlying_price = float(close.iloc[entry_index])
    exit_underlying_price = float(close.iloc[exit_index])
    strike = option_position["strike"]
    option_entry_price = option_position["entry_price"]
    entry_delta = option_position["entry_delta"]
    option_exit_price, exit_delta, exit_dte = option_mark_for_index(
        close,
        entry_index,
        exit_index,
        strike,
    )
    pnl_percent = (option_exit_price - option_entry_price) / option_entry_price
    pnl_dollars = (option_exit_price - option_entry_price) * CONTRACT_MULTIPLIER

    return {
        "symbol": symbol,
        "entry_date": close.index[entry_index].date().isoformat(),
        "exit_date": close.index[exit_index].date().isoformat(),
        "entry_dte": BACKTEST_ENTRY_DTE,
        "exit_dte": exit_dte,
        "estimated_strike": round(strike, 2),
        "estimated_entry_delta": round(entry_delta, 2),
        "estimated_exit_delta": round(exit_delta, 2),
        "entry_underlying_price": round(entry_underlying_price, 2),
        "exit_underlying_price": round(exit_underlying_price, 2),
        "estimated_option_entry_price": round(option_entry_price, 2),
        "estimated_option_exit_price": round(option_exit_price, 2),
        "pnl_dollars": round(pnl_dollars, 2),
        "pnl_percent": round(pnl_percent * 100, 2),
        "exit_reason": exit_reason,
    }


def backtest_close(symbol, close, max_entry_premium=None, strategy="current"):
    minimum_bars = max(MA_LONG, MACD_SLOW + MACD_SIGNAL) + 5
    if len(close) < minimum_bars:
        print(f"{symbol}: not enough historical data ({len(close)} bars)")
        return []

    indicators = (
        build_swing_signals(close) if strategy == "swing" else build_signals(close)
    )
    trades = []
    entry_index = None
    option_position = None
    underlying_low = None

    for index in range(minimum_bars, len(close)):
        if entry_index is None:
            enters = (
                is_swing_entry_at(close, indicators, index)
                if strategy == "swing"
                else is_bearish_at(close, indicators, index)
            )
            if enters:
                candidate = build_option_position(float(close.iloc[index]))
                entry_premium = candidate["entry_price"] * CONTRACT_MULTIPLIER
                if max_entry_premium is None or entry_premium <= max_entry_premium:
                    entry_index = index
                    option_position = candidate
                    underlying_low = float(close.iloc[index])
            continue

        option_entry_price = option_position["entry_price"]
        option_exit_price, _, _ = option_mark_for_index(
            close,
            entry_index,
            index,
            option_position["strike"],
        )
        option_pnl_pct = (option_exit_price - option_entry_price) / option_entry_price

        exit_reason = ""
        underlying_return = (
            float(close.iloc[entry_index]) - float(close.iloc[index])
        ) / float(close.iloc[entry_index])
        underlying_price = float(close.iloc[index])
        underlying_low = min(underlying_low, underlying_price)
        if option_pnl_pct <= -OPTION_STOP_LOSS_PERCENT:
            exit_reason = "option_stop_loss"
        elif option_pnl_pct >= OPTION_TAKE_PROFIT_PERCENT:
            exit_reason = "option_take_profit"
        elif underlying_price >= underlying_low * (1 + UNDERLYING_TRAILING_STOP_PERCENT):
            exit_reason = "underlying_trailing_stop"
        elif strategy == "swing" and underlying_return >= SWING_TAKE_PROFIT_PERCENT:
            exit_reason = "underlying_take_profit"
        elif index - entry_index >= (
            SWING_MAX_HOLDING_DAYS if strategy == "swing" else MAX_HOLDING_DAYS
        ):
            exit_reason = "max_holding_days"
        elif strategy == "swing" and float(close.iloc[index]) > float(
            indicators["ema_20"].iloc[index]
        ):
            exit_reason = "close_above_ema_20"
        else:
            exit_reason = (
                "" if strategy == "swing"
                else bullish_exit_reason(close, indicators, index)
            )

        if exit_reason:
            trades.append(build_trade(
                symbol,
                close,
                entry_index,
                index,
                exit_reason,
                option_position,
            ))
            entry_index = None
            option_position = None
            underlying_low = None

    if entry_index is not None:
        trades.append(build_trade(
            symbol,
            close,
            entry_index,
            len(close) - 1,
            "end_of_backtest",
            option_position,
        ))

    return trades


def backtest_symbol(symbol, period, interval, max_entry_premium=None, strategy="current"):
    close = get_close_series(symbol, period, interval)
    if close is None:
        print(f"{symbol}: no historical data")
        return []

    return backtest_close(symbol, close, max_entry_premium, strategy)


def save_trades(trades, results_file=RESULTS_FILE):
    results_file.parent.mkdir(parents=True, exist_ok=True)
    with results_file.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(trades)


def build_equity_curve(trades):
    equity = 0
    peak = 0
    equity_curve = []
    sorted_trades = sorted(trades, key=lambda trade: (trade["exit_date"], trade["symbol"]))

    for index, trade in enumerate(sorted_trades, start=1):
        pnl = float(trade["pnl_dollars"])
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity - peak
        equity_curve.append({
            "date": trade["exit_date"],
            "trade_number": index,
            "pnl_dollars": round(pnl, 2),
            "equity": round(equity, 2),
            "drawdown": round(drawdown, 2),
        })

    return equity_curve


def save_equity_curve(equity_curve, equity_curve_file=EQUITY_CURVE_FILE):
    equity_curve_file.parent.mkdir(parents=True, exist_ok=True)
    with equity_curve_file.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=EQUITY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(equity_curve)


def max_drawdown(equity_curve):
    if not equity_curve:
        return 0

    return abs(min(float(point["drawdown"]) for point in equity_curve))


def print_summary(trades, title="Options Backtest Summary"):
    print(f"\n{title}")
    print("=" * len(title))

    total_trades = len(trades)
    print(f"Total trades: {total_trades}")

    if not trades:
        print("Win rate: 0.00%")
        print("Total P/L: $0.00")
        print("Average win: $0.00")
        print("Average loss: $0.00")
        print("Profit factor: 0.00")
        print("Expectancy: $0.00/trade")
        print("Maximum drawdown: $0.00")
        print("Best symbol: n/a")
        print("Worst symbol: n/a")
        print("Trades by symbol: n/a")
        return

    pnl_values = [float(trade["pnl_dollars"]) for trade in trades]
    wins = [pnl for pnl in pnl_values if pnl > 0]
    losses = [pnl for pnl in pnl_values if pnl < 0]
    win_rate = len(wins) / total_trades
    total_pnl = sum(pnl_values)
    average_win = sum(wins) / len(wins) if wins else 0
    average_loss = sum(losses) / len(losses) if losses else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
    expectancy = total_pnl / total_trades
    equity_curve = build_equity_curve(trades)
    maximum_drawdown = max_drawdown(equity_curve)

    symbol_pnl = {}
    symbol_counts = {}
    for trade in trades:
        symbol = trade["symbol"]
        symbol_pnl[symbol] = symbol_pnl.get(symbol, 0) + float(trade["pnl_dollars"])
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

    best_symbol = max(symbol_pnl, key=symbol_pnl.get)
    worst_symbol = min(symbol_pnl, key=symbol_pnl.get)
    profit_factor_text = "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"

    print(f"Win rate: {win_rate:.2%}")
    print(f"Total P/L: ${total_pnl:.2f}")
    print(f"Average win: ${average_win:.2f}")
    print(f"Average loss: ${average_loss:.2f}")
    print(f"Profit factor: {profit_factor_text}")
    print(f"Expectancy: ${expectancy:.2f}/trade")
    print(f"Maximum drawdown: ${maximum_drawdown:.2f}")
    print(f"Best symbol: {best_symbol} (${symbol_pnl[best_symbol]:.2f})")
    print(f"Worst symbol: {worst_symbol} (${symbol_pnl[worst_symbol]:.2f})")
    print("Trades by symbol:")
    for symbol in sorted(symbol_counts):
        print(f"  {symbol}: {symbol_counts[symbol]} trades, ${symbol_pnl[symbol]:.2f} P/L")


def run_backtest(period, interval):
    regular_trades = []
    cheap_trades = []

    for symbol in UNDERLYINGS:
        print(f"Backtesting {symbol}...")
        close = get_close_series(symbol, period, interval)
        if close is None:
            print(f"{symbol}: no historical data")
            continue
        regular_trades.extend(backtest_close(symbol, close))
        cheap_trades.extend(backtest_close(
            symbol, close, CHEAP_MAX_PREMIUM, strategy="swing"
        ))

    regular_trades = apply_portfolio_constraints(
        regular_trades,
        max_entry_premium=REGULAR_MAX_PREMIUM_PER_TRADE,
    )
    cheap_trades = apply_portfolio_constraints(
        cheap_trades,
        max_entry_premium=CHEAP_MAX_PREMIUM,
    )
    regular_equity_curve = build_equity_curve(regular_trades)
    cheap_equity_curve = build_equity_curve(cheap_trades)
    save_trades(regular_trades, RESULTS_FILE)
    save_equity_curve(regular_equity_curve, EQUITY_CURVE_FILE)
    save_trades(cheap_trades, CHEAP_RESULTS_FILE)
    save_equity_curve(cheap_equity_curve, CHEAP_EQUITY_CURVE_FILE)

    print_summary(regular_trades, "Regular Options Backtest Summary")
    print_summary(cheap_trades, f"${CHEAP_MAX_PREMIUM:g} Daily Swing Backtest Summary")
    print(f"\nSaved regular trades to {RESULTS_FILE}")
    print(f"Saved regular equity curve to {EQUITY_CURVE_FILE}")
    print(f"Saved ${CHEAP_MAX_PREMIUM:g}-max trades to {CHEAP_RESULTS_FILE}")
    print(f"Saved ${CHEAP_MAX_PREMIUM:g}-max equity curve to {CHEAP_EQUITY_CURVE_FILE}")


def print_paper_results():
    from analytics import build_strategy_report

    strategy_names = [strategy["name"] for strategy in PAPER_STRATEGIES]
    report = build_strategy_report(strategy_names)
    print("\nLive Paper-Trading Results")
    print("==========================")
    print("Source: confirmed Alpaca paper fills in logs/trade_analytics.csv")

    for strategy in strategy_names:
        stats = report[strategy]
        completed = stats["completed_trades"]
        win_rate = stats["wins"] / completed if completed else 0
        print(f"\n{strategy}")
        print("-" * len(strategy))
        print(f"Completed trades: {completed}")
        print(f"Wins / losses: {stats['wins']} / {stats['losses']}")
        print(f"Win rate: {win_rate:.2%}")
        print(f"Realized P/L: ${stats['realized_pnl']:.2f}")
        print(f"Unrealized P/L: ${stats['unrealized_pnl']:.2f}")
        print(f"Total P/L: ${stats['realized_pnl'] + stats['unrealized_pnl']:.2f}")
        print(f"Open positions: {len(stats['open_positions'])}")
        print(f"Pending orders: {stats['pending_orders']}")
        for position in sorted(
            stats["open_positions"], key=lambda item: item["option_symbol"]
        ):
            current = position["current_price"]
            unrealized = position["unrealized_pnl"]
            current_text = f"${current:.2f}" if current is not None else "n/a"
            pnl_text = f"${unrealized:.2f}" if unrealized is not None else "n/a"
            print(
                f"  {position['option_symbol']}: qty={position['qty']:g}, "
                f"avg=${position['average_entry_price']:.2f}, "
                f"current={current_text}, unrealized={pnl_text}"
            )


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest the long put options strategy.")
    parser.add_argument(
        "--years",
        type=int,
        choices=sorted(YEARS_TO_PERIOD),
        default=1,
        help="Backtest length in years. Choices: 1, 3, 5. Default: 1",
    )
    parser.add_argument("--period", help="Optional yfinance period override, for example 3y")
    parser.add_argument("--interval", default="1d", help="yfinance interval to backtest. Default: 1d")
    parser.add_argument(
        "--paper-results",
        action="store_true",
        help="Show live Alpaca paper-fill performance without running a historical backtest.",
    )
    parser.add_argument(
        "--compare-signals",
        action="store_true",
        help="Compare current and experimental swing signals without synthetic option prices.",
    )
    parser.add_argument(
        "--alpaca-options",
        choices=("current", "swing"),
        help="Validate one signal using actual Alpaca historical daily option bars.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=100,
        help="Maximum recent candidates to query in --alpaca-options mode. Default: 100.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.paper_results:
        print_paper_results()
    elif args.alpaca_options:
        period = args.period or YEARS_TO_PERIOD[args.years]
        run_alpaca_option_backtest(
            period, args.interval, args.alpaca_options, args.max_candidates
        )
    elif args.compare_signals:
        period = args.period or YEARS_TO_PERIOD[args.years]
        run_signal_comparison(period, args.interval)
    else:
        period = args.period or YEARS_TO_PERIOD[args.years]
        run_backtest(period, args.interval)
