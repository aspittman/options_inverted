from datetime import date, datetime, timedelta
import re
import math
from types import SimpleNamespace
from uuid import uuid4

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
    AssetStatus,
    PositionIntent
)
from alpaca.common.exceptions import APIError
from requests.exceptions import RequestException

from config import (
    NON_CORPORATE_UNDERLYINGS,
    STRATEGY_ID, VIRTUAL_STARTING_CAPITAL, MAX_OPTION_PREMIUM_PER_TRADE,
    MAX_CONTRACTS_PER_TRADE, MIN_DTE, MAX_DTE, MAX_POSITIONS, MAX_POSITIONS_PER_CORRELATION_GROUP,
    correlation_group, ENABLE_NEW_ENTRIES,
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
from capital import capital_rejection, limit_price_at_mid
from research import (capital_snapshot, export_research_report, update_opportunity,
                      record_rejection, put_metadata, has_opportunity)
from bot_logger import bot_log

API_KEY, SECRET_KEY = require_alpaca_credentials()
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

_NON_CORPORATE_UNDERLYINGS = NON_CORPORATE_UNDERLYINGS
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
        result = float(value)
        return result if math.isfinite(result) else None
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
    """Return only confirmed owned long-put quantities, clipped to broker inventory."""
    try:
        lots = get_strategy_open_lots()
        owned = {}
        for (_, _, symbol), lot in lots.items():
            parsed = parse_option_symbol(symbol)
            if parsed and parsed["option_type"] == "put":
                qty, cost = owned.get(symbol, (0, 0))
                owned[symbol] = (qty + lot["qty"], cost + lot["cost"])
        result = []
        for position in trading_client.get_all_positions():
            if position.symbol not in owned:
                continue
            broker_qty = _to_float(getattr(position, "qty", None)) or 0
            if broker_qty <= 0:
                continue
            own_qty, cost = owned[position.symbol]
            qty = min(own_qty, broker_qty)
            avg = cost / own_qty
            current = _to_float(getattr(position, "current_price", None))
            if current is None:
                current = avg
            result.append(SimpleNamespace(symbol=position.symbol, qty=qty,
                qty_available=min(qty, _to_float(getattr(position, "qty_available", None)) or qty),
                avg_entry_price=avg, current_price=current, market_value=qty*current*100,
                unrealized_pl=(current-avg)*qty*100,
                unrealized_plpc=(current-avg)/avg if avg else 0))
        return result
    except Exception as exc:
        bot_log(f"Could not retrieve long_put positions: {exc}")
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
    report = export_research_report()
    bot_log(f"long_put virtual=${report['starting_virtual_capital']:,.0f} "
            f"equity=${report['ending_virtual_capital']:,.2f} "
            f"return={report['total_return_percent']:.2f}% "
            f"employed=${report['current_capital_employed']:,.2f}")
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
            f"divided by the ${VIRTUAL_STARTING_CAPITAL:,.0f} virtual allocation (variant rows are contributions)."
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

    # ETFs and trusts do not report corporate earnings. Asking Yahoo for an
    # earnings calendar produces a misleading "possibly delisted" error.
    if symbol in _NON_CORPORATE_UNDERLYINGS:
        return False

    cached = _earnings_cache.get((symbol, today, skip_days))
    if cached is not None:
        if cached:
            record_event("SKIP", underlying=underlying, reason="earnings_soon_or_unavailable",
                         details="cached_earnings_guard")
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

    if bid is None or ask is None or not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or ask < bid:
        return None

    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return None

    return (ask - bid) / midpoint


def contract_score(contract, snapshot, volume, underlying_price):
    delta = _to_float(getattr(getattr(snapshot, "greeks", None), "delta", None))
    spread_pct = bid_ask_spread_pct(getattr(snapshot, "latest_quote", None))

    if delta is None or not math.isfinite(delta) or spread_pct is None:
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
    quality_failures = []

    for contract in contracts:
        volume = volumes.get(contract.symbol, 0)
        if volume <= MIN_OPTION_VOLUME:
            quality_failures.append("INSUFFICIENT_LIQUIDITY")
            continue

        snapshot = snapshots.get(contract.symbol)
        if snapshot is None:
            quality_failures.append("NO_VALID_CONTRACT")
            continue

        score = contract_score(contract, snapshot, volume, underlying_price)
        if score is None:
            spread = bid_ask_spread_pct(getattr(snapshot, "latest_quote", None))
            quality_failures.append("SPREAD_TOO_WIDE" if spread is not None and spread >= MAX_BID_ASK_SPREAD_PCT else "NO_VALID_CONTRACT")
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
            reason=quality_failures[0] if len(set(quality_failures)) == 1 else "NO_VALID_CONTRACT",
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


def is_long_put_order(order):
    """Require explicit long-put intent; a strategy tag alone is insufficient."""
    try:
        parsed = parse_option_symbol(getattr(order, "symbol", ""))
    except (TypeError, ValueError):
        return False
    if not parsed or parsed["option_type"] != "put" or getattr(order, "legs", None):
        return False
    side = getattr(order, "side", None)
    side = getattr(side, "value", side)
    intent = getattr(order, "position_intent", None)
    intent = getattr(intent, "value", intent)
    return (side, intent) in {
        ("buy", "buy_to_open"),
        ("sell", "sell_to_close"),
    }


def reconcile_order_fills():
    """Poll only ledger-owned orders; reserve until confirmed terminal status."""
    for order_id, submitted in get_submitted_orders().items():
        try:
            order = trading_client.get_order_by_id(order_id)
            client_id = str(getattr(order, "client_order_id", ""))
            side = str(getattr(getattr(order, "side", ""), "value", getattr(order, "side", "")))
            if (not is_long_put_order(order) or
                    order.symbol != submitted.get("option_symbol") or
                    side != submitted.get("order_side") or
                    not client_id.startswith(("long_put_", "oi-"))):
                bot_log(f"Ignoring order with unverified long_put ownership: {order_id}")
                continue
            status = str(getattr(getattr(order, "status", ""), "value", order.status)).lower()
            total_qty = _to_float(getattr(order, "filled_qty", None)) or 0
            average = _to_float(getattr(order, "filled_avg_price", None)) or 0
            delta_qty = total_qty - float(submitted.get("recorded_fill_qty", 0))
            if delta_qty > 0 and average > 0:
                delta_price = (total_qty*average-float(submitted.get("recorded_fill_cost", 0)))/delta_qty
                record_event("ORDER_FILL", strategy=submitted.get("strategy", ""),
                    underlying=submitted.get("underlying", ""), option_symbol=order.symbol,
                    qty=delta_qty, price=delta_price,
                    underlying_price=submitted.get("underlying_price", ""), order_id=order_id,
                    order_side=submitted.get("order_side", ""), order_status=status,
                    details=submitted.get("details", ""))
            if status in {"filled", "canceled", "expired", "rejected", "failed"}:
                record_event("ORDER_TERMINAL", strategy=submitted.get("strategy", ""),
                    underlying=submitted.get("underlying", ""), option_symbol=order.symbol,
                    order_id=order_id, order_side=submitted.get("order_side", ""), order_status=status)
                if status == "rejected" and submitted.get("order_side") == "buy":
                    record_rejection("OTHER", variant=submitted.get("strategy", ""),
                        underlying=submitted.get("underlying", ""), contract_symbol=order.symbol,
                        details="broker_rejected_order")
                continue
            timeout = EXIT_LIMIT_TIMEOUT_MINUTES if submitted.get("order_side") == "sell" else LIMIT_ORDER_TIMEOUT_MINUTES
            opened = datetime.fromisoformat(submitted.get("timestamp", "").replace("Z", "+00:00"))
            now = datetime.now(opened.tzinfo) if opened.tzinfo else datetime.now()
            if timeout > 0 and now-opened >= timedelta(minutes=timeout) and status != "pending_cancel":
                trading_client.cancel_order_by_id(order_id)
                record_event("CANCEL_REQUESTED", strategy=submitted.get("strategy", ""),
                    underlying=submitted.get("underlying", ""), option_symbol=order.symbol,
                    order_id=order_id, reason="limit_order_timeout")
        except (ValueError, APIError, RequestException) as exc:
            bot_log(f"Could not reconcile owned order {order_id}: {exc}")


def bootstrap_legacy_positions():
    """Do not adopt account inventory: a historical symbol is not proof of ownership."""
    return None


def reconcile_strategy_lots_with_broker():
    """Reserve missing inventory until an owned exit or broker reconciliation."""
    try:
        broker_qty = {p.symbol: _to_float(p.qty) or 0 for p in trading_client.get_all_positions()}
    except Exception as exc:
        bot_log(f"Broker/ledger reconciliation skipped: {exc}")
        return
    lots = get_strategy_open_lots()
    totals = {}
    for (_, _, symbol), lot in lots.items():
        totals[symbol] = totals.get(symbol, 0) + lot["qty"]
    pending_sells = {row.get("option_symbol") for row in get_submitted_orders().values()
                     if row.get("order_side") == "sell"}
    for (variant, underlying, symbol), lot in lots.items():
        if symbol in pending_sells:
            continue
        matches = broker_qty.get(symbol, 0) >= totals[symbol]
        if matches and lot.get("unresolved"):
            record_event("POSITION_RECONCILED", strategy=variant, underlying=underlying,
                         option_symbol=symbol, details="owned_quantity_available_again")
        elif not matches and not lot.get("unresolved"):
            record_event("POSITION_MISSING", strategy=variant, underlying=underlying,
                option_symbol=symbol, qty=lot["qty"], reason="broker_quantity_below_owned",
                details="capital remains reserved; do not infer an exit or worthless expiration")


def buy_option_contract(
    option_symbol, qty=1, underlying="", strategy="regular", max_entry_premium=None,
    signal_date="",
):
    parsed = parse_option_symbol(option_symbol)
    if not parsed or parsed["option_type"] != "put" or qty <= 0:
        raise ValueError("Entries require a put contract and positive quantity")
    underlying = parsed["underlying"]
    if strategy not in {item["name"] for item in PAPER_STRATEGIES}:
        raise ValueError("Unknown long_put signal variant")
    if not ENABLE_NEW_ENTRIES:
        record_event("SKIP", strategy=strategy, underlying=underlying,
                     option_symbol=option_symbol, reason="new_entries_disabled")
        return False
    snapshot = get_option_snapshots([option_symbol]).get(option_symbol)
    quote = getattr(snapshot, "latest_quote", None)
    bid = _to_float(getattr(quote, "bid_price", None)) or 0
    ask = _to_float(getattr(quote, "ask_price", None)) or 0
    underlying_price = get_underlying_price(underlying) or ""
    state = capital_snapshot()
    update_opportunity(contract_symbol=option_symbol, bid=bid, ask=ask,
                       underlying_price=underlying_price, virtual_capital_available=state["available"])
    estimated_premium = None
    def reject(reason, detail=""):
        # Explicit calls also work outside the runtime opportunity context.
        if not has_opportunity():
            record_rejection(reason, variant=strategy, underlying=underlying,
                contract_symbol=option_symbol, bid=bid, ask=ask,
                virtual_capital_available=state["available"], details=detail,
                option_premium=estimated_premium, required_capital=estimated_premium,
                underlying_price=underlying_price)
        record_event("SKIP", strategy=strategy, underlying=underlying,
                     option_symbol=option_symbol, reason=reason, details=detail)
        return False
    if qty != MAX_CONTRACTS_PER_TRADE:
        return reject("MAX_CONTRACTS_REACHED")
    if not MIN_DTE <= (parsed["expiration"]-date.today()).days <= MAX_DTE:
        return reject("NO_VALID_CONTRACT", "entry_DTE_out_of_range")
    try:
        limit_price = limit_price_at_mid(bid, ask)
    except ValueError:
        return reject("OTHER", "invalid_entry_quote")
    if bid_ask_spread_pct(quote) >= MAX_BID_ASK_SPREAD_PCT:
        return reject("SPREAD_TOO_WIDE")
    delta = _to_float(getattr(getattr(snapshot, "greeks", None), "delta", None))
    if delta is None or not math.isfinite(delta) or abs(delta-TARGET_DELTA) > DELTA_TOLERANCE:
        return reject("NO_VALID_CONTRACT", "entry_delta_no_longer_qualifies")
    estimated_price = limit_price
    estimated_premium = limit_price * qty * CONTRACT_MULTIPLIER
    update_opportunity(option_premium=estimated_premium, required_capital=estimated_premium)
    variant_limit = next(item["max_premium"] for item in PAPER_STRATEGIES if item["name"] == strategy)
    premium_limit = min(MAX_OPTION_PREMIUM_PER_TRADE, variant_limit,
                        max_entry_premium if max_entry_premium is not None else MAX_OPTION_PREMIUM_PER_TRADE)
    lots = get_strategy_open_lots()
    pending = list(get_submitted_orders().values())
    buys = [row for row in pending if row.get("order_side") == "buy"]
    occupied = set(lots) | {(r.get("strategy"), r.get("underlying"), r.get("option_symbol")) for r in buys}
    duplicate = any(key[2] == option_symbol or (
        not ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING and key[1] == underlying) for key in occupied)
    if state["unresolved"]:
        return reject("OTHER", "unresolved_owned_position")
    try:
        # The shared account may net long and short positions in one OCC symbol.
        # Never open into another strategy's existing inventory or working order.
        if any(p.symbol == option_symbol for p in trading_client.get_all_positions()):
            return reject("DUPLICATE_POSITION", "shared_account_contract_conflict")
        if any(o.symbol == option_symbol for o in trading_client.get_orders()):
            return reject("DUPLICATE_POSITION", "shared_account_order_conflict")
    except (APIError, RequestException) as exc:
        return reject("OTHER", f"broker_ownership_check_failed:{type(exc).__name__}")

    reason = capital_rejection(estimated_premium, qty, premium_limit, state["available"],
        state["employed"], MAX_TOTAL_OPTION_PREMIUM, VIRTUAL_STARTING_CAPITAL,
        positions=len(occupied), max_positions=MAX_POSITIONS, duplicate=duplicate,
        group_count=sum(correlation_group(key[1]) == correlation_group(underlying) for key in occupied),
        max_group_positions=MAX_POSITIONS_PER_CORRELATION_GROUP)
    if reason:
        return reject(reason, f"premium={estimated_premium:.2f};limit={premium_limit:.2f}")

    order = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.BUY,
        position_intent=PositionIntent.BUY_TO_OPEN,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"long_put_{underlying}_{uuid4().hex[:16]}"
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
                     f"signal_date={signal_date};limit_price={limit_price:.2f};"
                     f"bid={bid};ask={ask};spread_pct={bid_ask_spread_pct(quote)}")
        )
        return True

    except (APIError, RequestException) as e:
        bot_log(f"Option order failed: {e}")
        record_event(
            "ORDER_FAILED",
            strategy=strategy,
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
    """Compatibility exit route: close confirmed lots, never account quantities."""
    for (variant, lot_underlying, symbol), lot in get_strategy_open_lots().items():
        if symbol == position.symbol and lot_underlying == underlying:
            close_strategy_lot(variant, underlying, symbol, lot["qty"], reason)


def close_strategy_lot(strategy, underlying, option_symbol, qty, reason):
    """Sell only the quantity assigned to one virtual strategy."""
    parsed = parse_option_symbol(option_symbol)
    owned = get_strategy_open_lots().get((strategy, underlying, option_symbol), {}).get("qty", 0)
    if not parsed or parsed["option_type"] != "put" or qty <= 0 or qty > owned:
        bot_log(f"Blocked exit without owned long-put quantity: {option_symbol}")
        return False
    # Legacy larger lots are unwound one contract per order as well.
    qty = min(qty, MAX_CONTRACTS_PER_TRADE)
    try:
        broker = next((p for p in trading_client.get_all_positions() if p.symbol == option_symbol), None)
        broker_qty = _to_float(getattr(broker, "qty", None)) or 0
        available = _to_float(getattr(broker, "qty_available", None))
        if available is None: available = broker_qty
        pending_sales = sum(float(r.get("remaining_qty", r.get("qty")) or 0)
            for r in get_submitted_orders().values()
            if r.get("option_symbol") == option_symbol and r.get("order_side") == "sell")
        if qty > min(broker_qty-pending_sales, available):
            return False
        if any(o.symbol == option_symbol for o in trading_client.get_orders()):
            return False
    except (APIError, RequestException) as exc:
        bot_log(f"Exit ownership verification failed: {exc}")
        return False
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
        position_intent=PositionIntent.SELL_TO_CLOSE,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"long_put_{underlying}_x_{uuid4().hex[:14]}",
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
