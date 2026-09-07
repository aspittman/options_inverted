from datetime import date, datetime, timedelta
import re

import yfinance as yf
from alpaca.data.enums import OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionBarsRequest,
    OptionSnapshotRequest,
    StockLatestTradeRequest
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ClosePositionRequest,
    LimitOrderRequest,
    GetOptionContractsRequest
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    ContractType,
    AssetStatus
)
from alpaca.common.exceptions import APIError
from requests.exceptions import RequestException

from config import (
    API_KEY,
    SECRET_KEY,
    ALPACA_PAPER,
    DELTA_TOLERANCE,
    EARNINGS_SKIP_DAYS,
    MAX_BID_ASK_SPREAD_PCT,
    MIN_OPEN_INTEREST,
    MIN_OPTION_VOLUME,
    OPTION_DATA_FEED,
    TARGET_DELTA,
    ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING,
    EXIT_DTE,
    MAX_TOTAL_OPTION_PREMIUM,
    OPTION_STOP_LOSS_PERCENT,
    OPTION_TRAILING_STOP_PERCENT,
    PAPER_STRATEGIES,
    BOT_PERFORMANCE_START_DATE,
    LIMIT_ORDER_TIMEOUT_MINUTES,
    EXIT_LIMIT_TIMEOUT_MINUTES,
    require_alpaca_credentials
)
from analytics import (
    get_owned_option_symbols,
    get_strategy_open_lots,
    get_underlying_low_water_marks,
    get_submitted_orders,
    record_event,
    summarize_results,
    summarize_performance_since,
)
from bot_logger import bot_log

API_KEY, SECRET_KEY = require_alpaca_credentials()
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

_NON_CORPORATE_UNDERLYINGS = {"SPY", "QQQ", "IWM", "DIA"}
_earnings_cache = {}


def _option_feed():
    feed = (OPTION_DATA_FEED or "").lower()
    if feed == "opra":
        return OptionsFeed.OPRA
    if feed == "indicative":
        return OptionsFeed.INDICATIVE
    return None


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calculate_underlying_trailing_stop(entry_price, current_price, prior_low, trail_pct):
    """Return a ratcheting low-water mark and its trailing stop price."""
    low_water = min(entry_price, current_price, prior_low or entry_price)
    return low_water, low_water * (1 + trail_pct)


OPTION_SYMBOL_RE = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")
CONTRACT_MULTIPLIER = 100
_option_high_water_marks = {}


def parse_option_symbol(symbol):
    match = OPTION_SYMBOL_RE.match(symbol or "")
    if not match:
        return None
    underlying, expiration, option_type, strike = match.groups()
    return {
        "underlying": underlying,
        "expiration": datetime.strptime(expiration, "%y%m%d").date(),
        "option_type": "call" if option_type == "C" else "put",
        "strike": int(strike) / 1000,
    }


def get_options_inverted_positions():
    try:
        owned = get_owned_option_symbols()
        return [
            position for position in trading_client.get_all_positions()
            if position.symbol in owned and parse_option_symbol(position.symbol)
        ]
    except Exception as e:
        bot_log(f"Could not retrieve OptionsInverted positions: {e}")
        return []


def get_open_positions_count():
    return len(get_options_inverted_positions())


def has_open_order(symbol):
    try:
        orders = trading_client.get_orders()
        return any(order.symbol == symbol for order in orders)
    except Exception:
        return False


def already_holding_underlying(underlying):
    return any(
        parse_option_symbol(position.symbol)["underlying"] == underlying
        for position in get_options_inverted_positions()
    )


def log_open_option_positions():
    positions = get_options_inverted_positions()
    bot_log(f"OptionsInverted open option positions: {len(positions)}")
    for position in positions:
        parsed = parse_option_symbol(position.symbol)
        qty = _to_float(getattr(position, "qty", None)) or 0
        avg = _to_float(getattr(position, "avg_entry_price", None)) or 0
        current = _to_float(getattr(position, "current_price", None)) or 0
        market_value = _to_float(getattr(position, "market_value", None))
        if market_value is None:
            market_value = qty * current * CONTRACT_MULTIPLIER
        pnl = _to_float(getattr(position, "unrealized_pl", None)) or 0
        pnl_pct = _to_float(getattr(position, "unrealized_plpc", None)) or 0
        dte = (parsed["expiration"] - date.today()).days
        bot_log(
            "OPEN_POSITION "
            f"contract={position.symbol} underlying={parsed['underlying']} "
            f"type={parsed['option_type']} strike={parsed['strike']:.3f} "
            f"expiration={parsed['expiration']} dte={dte} qty={qty:g} "
            f"avg_entry=${avg:.2f} current=${current:.2f} market_value=${market_value:.2f} "
            f"unrealized_pl=${pnl:.2f} unrealized_pl_pct={pnl_pct:.2%}"
        )
        record_event(
            "POSITION_SNAPSHOT", underlying=parsed["underlying"],
            option_symbol=position.symbol, qty=qty, price=current,
            unrealized_pnl=pnl,
            details=f"market_value={market_value:.2f};unrealized_pct={pnl_pct:.6f};dte={dte}"
        )
    return positions


def log_analytics_summary():
    results = summarize_results()
    for grouping, buckets in results.items():
        for symbol, values in sorted(buckets.items()):
            bot_log(
                f"RESULTS {grouping}={symbol} realized_pl=${values['realized_pnl']:.2f} "
                f"unrealized_pl=${values['unrealized_pnl']:.2f}"
            )


def _performance_table_row(label, premium_limit, result):
    """Format one human-readable row of the terminal performance summary."""
    limit = f"${premium_limit:,.0f}" if premium_limit is not None else "--"
    return (
        f"{label:<20} {limit:>11} "
        f"${result['deployed_premium']:>11,.2f} "
        f"${result['realized_pnl']:>10,.2f} "
        f"${result['unrealized_pnl']:>10,.2f} "
        f"${result['total_pnl']:>10,.2f} "
        f"{result['return_pct']:>+9.2f}%"
    )


def log_account_info(bot_positions=None):
    """Log whole-account balances and bot-only position/performance details."""
    try:
        account = trading_client.get_account()
        account_positions = trading_client.get_all_positions()
        bot_positions = (
            get_options_inverted_positions() if bot_positions is None else bot_positions
        )
        bot_market_value = sum(
            abs(_to_float(getattr(position, "market_value", None)) or 0)
            for position in bot_positions
        )
        account_positions_value = sum(
            abs(_to_float(getattr(position, "market_value", None)) or 0)
            for position in account_positions
        )
        current_prices = {
            position.symbol: _to_float(getattr(position, "current_price", None))
            for position in bot_positions
        }
        current_prices = {
            symbol: price for symbol, price in current_prices.items()
            if price is not None
        }
        performance = summarize_performance_since(
            BOT_PERFORMANCE_START_DATE, current_prices=current_prices
        )
        strategy_performance = {
            variant["name"]: summarize_performance_since(
                BOT_PERFORMANCE_START_DATE,
                current_prices=current_prices,
                strategy=variant["name"],
            )
            for variant in PAPER_STRATEGIES
        }
        cash = _to_float(getattr(account, "cash", None)) or 0
        equity = _to_float(getattr(account, "equity", None)) or 0
        buying_power = _to_float(getattr(account, "buying_power", None)) or 0

        bot_log("================ ACCOUNT & BOT PERFORMANCE ================")
        bot_log(
            f"ACCOUNT  Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}  |  "
            f"Buying power: ${buying_power:,.2f}"
        )
        bot_log(
            f"POSITIONS  Account: {len(account_positions)} (${account_positions_value:,.2f})  |  "
            f"OptionsInverted: {len(bot_positions)} (${bot_market_value:,.2f})"
        )
        bot_log(
            "LIMITS  "
            + "  |  ".join(
                f"{variant['name']}: ${(variant['max_premium'] or 0):,.0f}/trade"
                for variant in PAPER_STRATEGIES
            )
            + f"  |  Combined open premium: ${MAX_TOTAL_OPTION_PREMIUM:,.0f}"
        )
        bot_log("-" * 112)
        bot_log(
            f"{'PERFORMANCE':<20} {'TRADE CAP':>11} {'DEPLOYED':>12} "
            f"{'REALIZED':>11} {'UNREALIZED':>11} {'TOTAL P/L':>11} {'GAIN/LOSS':>10}"
        )
        bot_log("-" * 112)
        for variant in PAPER_STRATEGIES:
            name = variant["name"]
            result = strategy_performance[name]
            bot_log(
                _performance_table_row(
                    name, variant["max_premium"], result
                )
                + f"  ({result['open_positions']} open)"
            )
        bot_log(
            _performance_table_row(
                "COMBINED PORTFOLIO", MAX_TOTAL_OPTION_PREMIUM, performance
            )
        )
        bot_log(
            f"Period starts {performance['start_date']}. Gain/loss is total P/L "
            "divided by premium deployed."
        )
        bot_log("===========================================================")
        return performance
    except Exception as exc:
        bot_log(f"Could not log account info: {exc}")
        return None


def has_earnings_soon(underlying, skip_days=EARNINGS_SKIP_DAYS):
    today = date.today()
    last_skip_date = today + timedelta(days=skip_days)
    symbol = underlying.upper()

    # Broad-market ETFs do not report corporate earnings. Asking Yahoo for an
    # earnings calendar produces a misleading "possibly delisted" error.
    if symbol in _NON_CORPORATE_UNDERLYINGS:
        return False

    cached = _earnings_cache.get((symbol, today, skip_days))
    if cached is not None:
        return cached

    try:
        earnings = yf.Ticker(symbol).get_earnings_dates(limit=12)

        if earnings is None or earnings.empty:
            _earnings_cache[(symbol, today, skip_days)] = False
            return False

        for earnings_date in earnings.index:
            if isinstance(earnings_date, datetime):
                earnings_day = earnings_date.date()
            else:
                earnings_day = earnings_date

            if today <= earnings_day <= last_skip_date:
                bot_log(f"{underlying}: earnings on {earnings_day}. Skipping.")
                record_event(
                    "SKIP",
                    underlying=underlying,
                    reason="earnings_soon",
                    details=f"earnings_date={earnings_day}"
                )
                _earnings_cache[(symbol, today, skip_days)] = True
                return True

        _earnings_cache[(symbol, today, skip_days)] = False
        return False

    except Exception as e:
        bot_log(f"Could not check earnings for {underlying}: {e}")
        record_event(
            "SKIP",
            underlying=underlying,
            reason="earnings_check_failed",
            details=f"error={type(e).__name__}",
        )
        # Earnings protection is a risk control. If its data source or parser is
        # unavailable, do not assume that opening a new position is safe.
        return True


def get_underlying_price(underlying):
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=underlying)
        trade = stock_data_client.get_stock_latest_trade(request)

        if isinstance(trade, dict):
            trade = trade.get(underlying)

        price = _to_float(getattr(trade, "price", None))
        if price and price > 0:
            return price

    except Exception as e:
        bot_log(f"Could not get Alpaca latest trade for {underlying}: {e}")

    return None


def get_option_volumes(symbols):
    if not symbols:
        return {}

    try:
        request = OptionBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=datetime.combine(date.today(), datetime.min.time()),
            feed=_option_feed()
        )
        bars = option_data_client.get_option_bars(request)
        volumes = {}

        for symbol in symbols:
            symbol_bars = bars.data.get(symbol, [])
            volumes[symbol] = int(symbol_bars[-1].volume or 0) if symbol_bars else 0

        return volumes

    except Exception as e:
        bot_log(f"Could not get option volumes: {e}")
        return {}


def get_option_snapshots(symbols):
    if not symbols:
        return {}

    try:
        request = OptionSnapshotRequest(
            symbol_or_symbols=symbols,
            feed=_option_feed()
        )
        return option_data_client.get_option_snapshot(request)

    except Exception as e:
        bot_log(f"Could not get option snapshots: {e}")
        return {}


def bid_ask_spread_pct(quote):
    bid = _to_float(getattr(quote, "bid_price", None))
    ask = _to_float(getattr(quote, "ask_price", None))

    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None

    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return None

    return (ask - bid) / midpoint


def contract_score(contract, snapshot, volume, underlying_price):
    delta = _to_float(getattr(getattr(snapshot, "greeks", None), "delta", None))
    spread_pct = bid_ask_spread_pct(getattr(snapshot, "latest_quote", None))

    if delta is None or spread_pct is None:
        return None

    if abs(delta - TARGET_DELTA) > DELTA_TOLERANCE:
        return None

    if spread_pct >= MAX_BID_ASK_SPREAD_PCT:
        return None

    dte = (contract.expiration_date - date.today()).days
    strike_distance = abs(_to_float(contract.strike_price) - underlying_price)

    return (
        abs(delta - TARGET_DELTA),
        spread_pct,
        -volume,
        dte,
        strike_distance
    )


def get_option_contract(underlying, option_type="put", min_dte=60, max_dte=90):
    today = date.today()
    min_exp = today + timedelta(days=min_dte)
    max_exp = today + timedelta(days=max_dte)
    underlying_price = get_underlying_price(underlying)

    if not underlying_price:
        bot_log(f"Could not determine underlying price for {underlying}")
        record_event("SKIP", underlying=underlying, reason="missing_underlying_price")
        return None

    if option_type != "put":
        raise ValueError("OptionsInverted only trades long puts")
    contract_type = ContractType.PUT

    request = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=min_exp,
        expiration_date_lte=max_exp,
        type=contract_type,
        # alpaca-py models strike filters as strings (even though they contain
        # numeric values). Passing floats fails Pydantic validation before the
        # API request is made.
        strike_price_gte=f"{underlying_price * 0.85:.2f}",
        strike_price_lte=f"{underlying_price * 1.15:.2f}",
        limit=1000
    )

    contracts = []

    try:
        while True:
            response = trading_client.get_option_contracts(request)
            contracts.extend(response.option_contracts or [])

            if not response.next_page_token:
                break

            request.page_token = response.next_page_token

    except (APIError, RequestException) as e:
        bot_log(f"Could not get option contracts for {underlying}: {e}")
        record_event(
            "SKIP",
            underlying=underlying,
            reason="contract_lookup_failed",
            details=str(e)
        )
        return None

    if not contracts:
        bot_log(f"No option contracts found for {underlying}")
        record_event("SKIP", underlying=underlying, reason="no_contracts")
        return None

    contracts = [
        contract
        for contract in contracts
        if contract.tradable
        and min_dte <= (contract.expiration_date - today).days <= max_dte
        and (_to_float(contract.open_interest) or 0) > MIN_OPEN_INTEREST
    ]

    if not contracts:
        bot_log(f"No {underlying} contracts passed open interest > {MIN_OPEN_INTEREST}")
        record_event("SKIP", underlying=underlying, reason="open_interest_filter")
        return None

    symbols = [contract.symbol for contract in contracts]
    snapshots = get_option_snapshots(symbols)
    volumes = get_option_volumes(symbols)
    ranked = []

    for contract in contracts:
        volume = volumes.get(contract.symbol, 0)
        if volume <= MIN_OPTION_VOLUME:
            continue

        snapshot = snapshots.get(contract.symbol)
        if snapshot is None:
            continue

        score = contract_score(contract, snapshot, volume, underlying_price)
        if score is None:
            continue

        ranked.append((score, contract, snapshot, volume))

    if not ranked:
        bot_log(
            f"No {underlying} contracts passed volume > {MIN_OPTION_VOLUME}, "
            f"spread < {MAX_BID_ASK_SPREAD_PCT:.0%}, and delta near {TARGET_DELTA:.2f}"
        )
        record_event(
            "SKIP",
            underlying=underlying,
            reason="contract_quality_filters",
            details=(
                f"min_volume={MIN_OPTION_VOLUME};"
                f"max_spread={MAX_BID_ASK_SPREAD_PCT};"
                f"target_delta={TARGET_DELTA};"
                f"delta_tolerance={DELTA_TOLERANCE}"
            )
        )
        return None

    ranked.sort(key=lambda item: item[0])
    _, selected, selected_snapshot, selected_volume = ranked[0]
    greeks = getattr(selected_snapshot, "greeks", None)
    quote = getattr(selected_snapshot, "latest_quote", None)
    bid = _to_float(getattr(quote, "bid_price", None)) or 0
    ask = _to_float(getattr(quote, "ask_price", None)) or 0
    midpoint = (bid + ask) / 2
    spread_dollars = ask - bid
    selected_spread = bid_ask_spread_pct(quote) or 0
    selected_delta = _to_float(getattr(greeks, "delta", None))
    gamma = _to_float(getattr(greeks, "gamma", None))
    theta = _to_float(getattr(greeks, "theta", None))
    iv = _to_float(getattr(selected_snapshot, "implied_volatility", None))
    dte = (selected.expiration_date - today).days
    selection_reason = (
        "best rank by delta distance, spread, volume, DTE, and strike distance "
        "after unchanged liquidity and Greek filters"
    )
    bot_log(
        "CONTRACT_SELECTED "
        f"underlying={underlying} underlying_price=${underlying_price:.2f} "
        f"contract={selected.symbol} strike={float(selected.strike_price):.3f} "
        f"expiration={selected.expiration_date} dte={dte} bid=${bid:.2f} ask=${ask:.2f} "
        f"midpoint=${midpoint:.2f} spread_dollars=${spread_dollars:.2f} "
        f"spread_pct={selected_spread:.2%} delta={selected_delta:.4f} "
        f"gamma={gamma if gamma is not None else 'N/A'} theta={theta if theta is not None else 'N/A'} "
        f"iv={iv if iv is not None else 'N/A'} volume={selected_volume} "
        f"open_interest={selected.open_interest} reason=\"{selection_reason}\""
    )
    record_event(
        "CONTRACT_SELECTED",
        underlying=underlying,
        option_symbol=selected.symbol,
        price=underlying_price,
        details=(
            f"underlying_price={underlying_price};expiration={selected.expiration_date};dte={dte};"
            f"strike={selected.strike_price};"
            f"open_interest={selected.open_interest};"
            f"volume={selected_volume};"
            f"bid={bid};ask={ask};midpoint={midpoint};spread_dollars={spread_dollars};"
            f"spread_pct={selected_spread:.6f};delta={selected_delta:.6f};"
            f"gamma={gamma};theta={theta};iv={iv};selection_reason={selection_reason}"
        )
    )

    return selected.symbol


def _strategy_has_pending_order(strategy, option_symbol, side=None):
    for row in get_submitted_orders().values():
        if row.get("strategy") != strategy or row.get("option_symbol") != option_symbol:
            continue
        if side is None or row.get("order_side") == side:
            return True
    return False


def reconcile_order_fills():
    """Record confirmed Alpaca fills so performance uses paper execution prices."""
    for order_id, submitted in get_submitted_orders().items():
        try:
            order = trading_client.get_order_by_id(order_id)
        except (APIError, RequestException) as exc:
            bot_log(f"Could not reconcile order {order_id}: {exc}")
            continue
        raw_status = getattr(order, "status", "")
        status = str(getattr(raw_status, "value", raw_status))
        filled_qty = _to_float(getattr(order, "filled_qty", None)) or 0
        fill_price = _to_float(getattr(order, "filled_avg_price", None)) or 0
        terminal_statuses = {"canceled", "expired", "rejected", "failed"}
        if (
            status.lower() == "filled" or status.lower() in terminal_statuses
        ) and filled_qty > 0 and fill_price > 0:
            record_event(
                "ORDER_FILL",
                strategy=submitted.get("strategy", ""),
                underlying=submitted.get("underlying", ""),
                option_symbol=submitted.get("option_symbol", ""),
                qty=filled_qty,
                price=fill_price,
                underlying_price=submitted.get("underlying_price", ""),
                order_id=order_id,
                order_side=submitted.get("order_side", ""),
                order_status=status,
            )
            bot_log(
                f"PAPER_FILL strategy={submitted.get('strategy')} side={submitted.get('order_side')} "
                f"contract={submitted.get('option_symbol')} qty={filled_qty:g} price=${fill_price:.2f}"
            )
        elif status.lower() in terminal_statuses:
            record_event(
                "ORDER_TERMINAL",
                strategy=submitted.get("strategy", ""),
                underlying=submitted.get("underlying", ""),
                option_symbol=submitted.get("option_symbol", ""),
                order_id=order_id,
                order_side=submitted.get("order_side", ""),
                order_status=status,
                reason="unfilled_terminal_order",
            )
        else:
            timeout_minutes = (
                EXIT_LIMIT_TIMEOUT_MINUTES
                if submitted.get("order_side") == "sell"
                else LIMIT_ORDER_TIMEOUT_MINUTES
            )
            if timeout_minutes <= 0:
                continue
            try:
                submitted_at = datetime.fromisoformat(
                    submitted.get("timestamp", "").replace("Z", "+00:00")
                )
                now = datetime.now(submitted_at.tzinfo) if submitted_at.tzinfo else datetime.now()
                age = now - submitted_at
                if age >= timedelta(minutes=timeout_minutes):
                    trading_client.cancel_order_by_id(order_id)
                    record_event(
                        "ORDER_TERMINAL", strategy=submitted.get("strategy", ""),
                        underlying=submitted.get("underlying", ""),
                        option_symbol=submitted.get("option_symbol", ""),
                        order_id=order_id, order_side=submitted.get("order_side", ""),
                        order_status="cancel_requested", reason="limit_order_timeout",
                    )
                    bot_log(f"Canceled stale unfilled limit order {order_id}")
            except (ValueError, APIError, RequestException) as exc:
                bot_log(f"Could not cancel stale order {order_id}: {exc}")


def bootstrap_legacy_positions():
    """Adopt pre-ledger bot positions as regular lots without double-counting pending buys."""
    lots = get_strategy_open_lots()
    tracked_qty = {}
    for (_, _, symbol), lot in lots.items():
        tracked_qty[symbol] = tracked_qty.get(symbol, 0) + lot["qty"]
    pending_symbols = {
        row.get("option_symbol", "") for row in get_submitted_orders().values()
        if row.get("order_side") == "buy"
    }
    for position in get_options_inverted_positions():
        symbol = position.symbol
        account_qty = _to_float(getattr(position, "qty", None)) or 0
        missing_qty = account_qty - tracked_qty.get(symbol, 0)
        if missing_qty <= 0 or symbol in pending_symbols:
            continue
        parsed = parse_option_symbol(symbol)
        entry_price = _to_float(getattr(position, "avg_entry_price", None)) or 0
        if not parsed or entry_price <= 0:
            continue
        underlying = parsed["underlying"]
        record_event(
            "ORDER_FILL", strategy="regular", underlying=underlying,
            option_symbol=symbol, qty=missing_qty, price=entry_price,
            underlying_price=get_underlying_price(underlying) or "",
            order_id=f"legacy-{symbol}", order_side="buy", order_status="filled",
            details="adopted pre-strategy-ledger paper position",
        )
        bot_log(f"Adopted legacy paper position as regular: {symbol} qty={missing_qty:g}")


def reconcile_strategy_lots_with_broker():
    """Stop stale virtual lots from surviving after the broker position is gone."""
    try:
        account_symbols = {
            position.symbol for position in trading_client.get_all_positions()
            if parse_option_symbol(position.symbol)
        }
    except Exception as exc:
        bot_log(f"Broker/ledger reconciliation skipped: {exc}")
        return
    pending_sells = {
        (row.get("strategy", ""), row.get("option_symbol", ""))
        for row in get_submitted_orders().values()
        if row.get("order_side") == "sell"
    }
    for (strategy, underlying, symbol), lot in get_strategy_open_lots().items():
        if symbol in account_symbols or (strategy, symbol) in pending_sells:
            continue
        record_event(
            "POSITION_MISSING", strategy=strategy, underlying=underlying,
            option_symbol=symbol, qty=lot["qty"],
            reason="broker_position_absent",
            details="virtual lot cleared without realized P/L; broker is authoritative",
        )
        bot_log(
            f"Cleared stale virtual lot strategy={strategy} contract={symbol}: "
            "position absent at broker"
        )


def buy_option_contract(
    option_symbol, qty=1, underlying="", strategy="regular", max_entry_premium=None,
    signal_date="",
):
    parsed = parse_option_symbol(option_symbol)
    if not parsed or parsed["option_type"] != "put" or qty <= 0:
        raise ValueError("Entries require a put contract and positive quantity")
    positions = get_options_inverted_positions()
    strategy_lots = get_strategy_open_lots()
    strategy_holds_symbol = any(
        lot_strategy == strategy and symbol == option_symbol
        for lot_strategy, _, symbol in strategy_lots
    )
    if strategy_holds_symbol or _strategy_has_pending_order(strategy, option_symbol, "buy"):
        bot_log(f"Duplicate contract blocked: {option_symbol}")
        record_event("SKIP", strategy=strategy, underlying=underlying, option_symbol=option_symbol, reason="duplicate_contract")
        return
    if (
        not ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING
        and parsed
        and any(lot_strategy == strategy and lot_underlying == underlying
                for lot_strategy, lot_underlying, _ in strategy_lots)
    ):
        bot_log(f"Additional contract for strategy={strategy} {underlying} blocked by configuration.")
        record_event("SKIP", strategy=strategy, underlying=underlying, option_symbol=option_symbol, reason="multiple_underlying_contracts")
        return

    snapshots = get_option_snapshots([option_symbol])
    snapshot = snapshots.get(option_symbol)
    quote = getattr(snapshot, "latest_quote", None)
    bid = _to_float(getattr(quote, "bid_price", None)) or 0
    ask = _to_float(getattr(quote, "ask_price", None)) or 0
    estimated_price = (bid + ask) / 2 if bid > 0 and ask > 0 else ask
    estimated_premium = estimated_price * qty * CONTRACT_MULTIPLIER
    current_total = sum(
        (_to_float(getattr(p, "avg_entry_price", None)) or 0)
        * abs(_to_float(getattr(p, "qty", None)) or 0) * CONTRACT_MULTIPLIER
        for p in positions
    )
    if estimated_premium <= 0:
        bot_log(f"Cannot price {option_symbol} for premium risk checks. Skipping.")
        record_event("SKIP", underlying=underlying, option_symbol=option_symbol, reason="missing_option_price")
        return
    if max_entry_premium is not None and estimated_premium > max_entry_premium:
        bot_log(f"Premium limit blocked strategy={strategy} {option_symbol}: estimated=${estimated_premium:.2f}, limit=${max_entry_premium:.2f}")
        record_event("SKIP", strategy=strategy, underlying=underlying, option_symbol=option_symbol, reason="max_premium_per_trade", details=f"estimated_premium={estimated_premium:.2f};limit={max_entry_premium:.2f}")
        return
    if current_total + estimated_premium > MAX_TOTAL_OPTION_PREMIUM:
        bot_log(f"Total premium limit blocked {option_symbol}: current=${current_total:.2f}, proposed=${estimated_premium:.2f}, MAX_TOTAL_OPTION_PREMIUM=${MAX_TOTAL_OPTION_PREMIUM:.2f}")
        record_event("SKIP", underlying=underlying, option_symbol=option_symbol, reason="max_total_option_premium")
        return

    limit_price = round(estimated_price, 2)
    order = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"oi-{strategy}-{int(datetime.now().timestamp() * 1000)}"
    )

    try:
        submitted_order = trading_client.submit_order(order)
        underlying_price = get_underlying_price(underlying) if underlying else ""
        bot_log(f"Placed midpoint LIMIT BUY for {qty} {option_symbol} at ${limit_price:.2f}")
        record_event(
            "ORDER_SUBMITTED",
            strategy=strategy,
            underlying=underlying,
            option_symbol=option_symbol,
            qty=qty,
            price=estimated_price,
            underlying_price=underlying_price,
            order_id=str(getattr(submitted_order, "id", "")),
            order_side="buy",
            order_status=str(getattr(submitted_order, "status", "")),
            details=(f"order_id={getattr(submitted_order, 'id', '')};"
                     f"underlying_price={underlying_price};estimated_premium={estimated_premium:.2f};"
                     f"signal_date={signal_date};limit_price={limit_price:.2f}")
        )
        return True

    except (APIError, RequestException) as e:
        bot_log(f"Option order failed: {e}")
        record_event(
            "ORDER_FAILED",
            underlying=underlying,
            option_symbol=option_symbol,
            qty=qty,
            reason="buy_failed",
            details=str(e)
        )
        return False


def get_option_positions_for_underlying(underlying):
    return [
        position for position in get_options_inverted_positions()
        if parse_option_symbol(position.symbol)["underlying"] == underlying
    ]


def close_option_position(position, underlying, reason):
    if has_open_order(position.symbol):
        bot_log(f"Open order exists for {position.symbol}. Exit skipped.")
        return

    qty = getattr(position, "qty_available", None) or getattr(position, "qty", None)
    qty_float = _to_float(qty)

    if qty_float is not None and qty_float <= 0:
        bot_log(f"No available quantity to exit for {position.symbol}.")
        return

    try:
        close_request = ClosePositionRequest(qty=qty) if qty else None
        submitted_order = trading_client.close_position(position.symbol, close_request)
        bot_log(f"Submitted exit for {position.symbol}: {reason}")
        record_event(
            "EXIT_SUBMITTED",
            underlying=underlying,
            option_symbol=position.symbol,
            qty=qty,
            price=get_underlying_price(underlying) or "",
            unrealized_pnl=_to_float(getattr(position, "unrealized_pl", None)) or 0,
            reason=reason,
            details=f"order_id={getattr(submitted_order, 'id', '')}"
        )

    except (APIError, RequestException) as e:
        bot_log(f"Exit failed for {position.symbol}: {e}")
        record_event(
            "ORDER_FAILED",
            underlying=underlying,
            option_symbol=position.symbol,
            qty=qty,
            reason="exit_failed",
            details=str(e)
        )


def close_strategy_lot(strategy, underlying, option_symbol, qty, reason):
    """Sell only the quantity assigned to one virtual strategy."""
    if _strategy_has_pending_order(strategy, option_symbol, "sell"):
        return
    snapshot = get_option_snapshots([option_symbol]).get(option_symbol)
    quote = getattr(snapshot, "latest_quote", None)
    bid = _to_float(getattr(quote, "bid_price", None)) or 0
    ask = _to_float(getattr(quote, "ask_price", None)) or 0
    if bid <= 0 or ask <= 0 or ask < bid:
        bot_log(f"Cannot place safe limit exit for {option_symbol}: invalid quote")
        record_event(
            "SKIP", strategy=strategy, underlying=underlying,
            option_symbol=option_symbol, reason="invalid_exit_quote"
        )
        return
    # Once risk has triggered, prioritize execution. A sell limit at the live
    # bid is normally marketable while still enforcing a minimum sale price.
    limit_price = round(bid, 2)
    order = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"oi-{strategy}-x-{int(datetime.now().timestamp() * 1000)}",
    )
    try:
        submitted = trading_client.submit_order(order)
        record_event(
            "ORDER_SUBMITTED",
            strategy=strategy,
            underlying=underlying,
            option_symbol=option_symbol,
            qty=qty,
            underlying_price=get_underlying_price(underlying) or "",
            reason=reason,
            order_id=str(getattr(submitted, "id", "")),
            order_side="sell",
            order_status=str(getattr(submitted, "status", "")),
            details=f"limit_price={limit_price:.2f}",
        )
        bot_log(
            f"Submitted marketable LIMIT SELL strategy={strategy} contract={option_symbol} "
            f"qty={qty:g} limit=${limit_price:.2f}: {reason}"
        )
    except (APIError, RequestException) as exc:
        bot_log(f"Strategy exit failed strategy={strategy} contract={option_symbol}: {exc}")
        record_event(
            "ORDER_FAILED", strategy=strategy, underlying=underlying,
            option_symbol=option_symbol, qty=qty, reason="exit_failed", details=str(exc)
        )


def manage_underlying_exits(
    underlyings,
    exit_signal_func,
    trailing_stop_pct,
    take_profit_pct
):
    positions_by_symbol = {
        position.symbol: position for position in get_options_inverted_positions()
    }
    lots = get_strategy_open_lots()
    underlying_low_water_marks = get_underlying_low_water_marks()
    for underlying in underlyings:
        underlying_lots = [
            (key, lot) for key, lot in lots.items() if key[1] == underlying
        ]
        if not underlying_lots:
            continue

        current_price = get_underlying_price(underlying)

        for (strategy, _, option_symbol), lot in underlying_lots:
            variant = next(
                (item for item in PAPER_STRATEGIES if item["name"] == strategy),
                {
                    "signal": "daily_trend",
                    "underlying_trailing_stop": trailing_stop_pct,
                    "underlying_take_profit": take_profit_pct,
                    "max_holding_days": 20,
                },
            )
            technical_exit, technical_reason = exit_signal_func(
                underlying, variant["signal"]
            )
            position = positions_by_symbol.get(option_symbol)
            if position is None:
                continue
            exit_reason = technical_reason if technical_exit else ""
            entry_price = lot["underlying_cost"] / lot["qty"] if lot["qty"] else 0
            parsed = parse_option_symbol(option_symbol)
            dte = (parsed["expiration"] - date.today()).days
            option_price = _to_float(getattr(position, "current_price", None)) or 0
            option_entry = lot["cost"] / lot["qty"] if lot["qty"] else 0
            option_plpc = (option_price - option_entry) / option_entry if option_entry else 0
            opened_at = lot.get("opened_at", "")
            held_weekdays = 0
            if opened_at:
                try:
                    opened_date = datetime.fromisoformat(
                        opened_at.replace("Z", "+00:00")
                    ).date()
                    cursor = opened_date
                    while cursor < date.today():
                        cursor += timedelta(days=1)
                        if cursor.weekday() < 5:
                            held_weekdays += 1
                except ValueError:
                    bot_log(
                        f"Could not parse entry timestamp for {option_symbol}: {opened_at}"
                    )
            option_high_water_key = (strategy, option_symbol)
            option_high_water = max(
                _option_high_water_marks.get(option_high_water_key, option_price),
                option_price,
            )
            _option_high_water_marks[option_high_water_key] = option_high_water

            if dte <= EXIT_DTE:
                exit_reason = f"expiration_management_dte_{dte}"
            elif option_plpc <= -OPTION_STOP_LOSS_PERCENT:
                exit_reason = f"option_stop_loss_{option_plpc:.2%}"
            elif held_weekdays >= variant["max_holding_days"]:
                exit_reason = f"max_holding_days_{held_weekdays}"
            elif (
                OPTION_TRAILING_STOP_PERCENT > 0
                and option_high_water > 0
                and option_price <= option_high_water * (1 - OPTION_TRAILING_STOP_PERCENT)
            ):
                drawdown = (option_price - option_high_water) / option_high_water
                exit_reason = f"option_trailing_stop_{drawdown:.2%}"

            if current_price and entry_price:
                risk_key = (strategy, underlying, option_symbol)
                previous_low = underlying_low_water_marks.get(risk_key, entry_price)
                trailing_pct = variant["underlying_trailing_stop"]
                underlying_low, trailing_stop = calculate_underlying_trailing_stop(
                    entry_price, current_price, previous_low, trailing_pct
                )
                underlying_low_water_marks[risk_key] = underlying_low
                record_event(
                    "RISK_SNAPSHOT",
                    strategy=strategy,
                    underlying=underlying,
                    option_symbol=option_symbol,
                    qty=lot["qty"],
                    price=option_price,
                    underlying_price=current_price,
                    details=(
                        f"underlying_low_water={underlying_low:.6f};"
                        f"underlying_trailing_stop={trailing_stop:.6f};"
                        f"trailing_percent={trailing_pct:.6f}"
                    ),
                )

                if current_price >= trailing_stop:
                    drawdown = (current_price - underlying_low) / underlying_low
                    exit_reason = f"underlying_trailing_stop_{drawdown:.2%}"
                elif (entry_price - current_price) / entry_price >= variant["underlying_take_profit"]:
                    change_pct = (entry_price - current_price) / entry_price
                    exit_reason = f"underlying_take_profit_{change_pct:.2%}"

            if exit_reason:
                close_strategy_lot(
                    strategy, underlying, option_symbol, lot["qty"], exit_reason
                )

    open_keys = {(strategy, symbol) for strategy, _, symbol in lots}
    for key in list(_option_high_water_marks):
        if key not in open_keys:
            del _option_high_water_marks[key]
