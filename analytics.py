import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from config import ANALYTICS_FILE, STRATEGY_ID, VIRTUAL_STARTING_CAPITAL
from research import is_own_event, observe_event


FIELDNAMES = [
    "bot_strategy",
    "timestamp",
    "strategy",
    "event",
    "underlying",
    "option_symbol",
    "qty",
    "price",
    "underlying_price",
    "realized_pnl",
    "unrealized_pnl",
    "reason",
    "details",
    "order_id",
    "order_side",
    "order_status",
]


def _ensure_schema(path):
    if not path.exists():
        return
    with path.open("r", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames == FIELDNAMES:
            return
        rows = list(reader)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def record_event(
    event,
    strategy="",
    underlying="",
    option_symbol="",
    qty="",
    price="",
    underlying_price="",
    realized_pnl="",
    unrealized_pnl="",
    reason="",
    details="",
    order_id="",
    order_side="",
    order_status="",
):
    observe_event(event, strategy=strategy, underlying=underlying,
                  option_symbol=option_symbol, reason=reason, details=details)
    path = Path(ANALYTICS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_schema(path)
    write_header = not path.exists()

    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)

        if write_header:
            writer.writeheader()

        writer.writerow({
            "bot_strategy": STRATEGY_ID,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "strategy": strategy,
            "event": event,
            "underlying": underlying,
            "option_symbol": option_symbol,
            "qty": qty,
            "price": price,
            "underlying_price": underlying_price,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "reason": reason,
            "details": details,
            "order_id": order_id,
            "order_side": order_side,
            "order_status": order_status,
        })


def read_events():
    path = Path(ANALYTICS_FILE)
    if not path.exists():
        return []
    _ensure_schema(path)
    with path.open("r", newline="") as file:
        return [row for row in csv.DictReader(file) if is_own_event(row)]


def latest_strategy_exit_date(strategy, underlying):
    latest = None
    for row in read_events():
        if (
            row.get("event") in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}
            and row.get("order_side") == "sell"
            and row.get("strategy") == strategy
            and row.get("underlying") == underlying
        ):
            try:
                day = datetime.fromisoformat(row.get("timestamp", "")).date()
            except ValueError:
                continue
            latest = max(latest, day) if latest else day
    return latest


def cooldown_active(strategy, underlying, trading_days, today=None):
    exited = latest_strategy_exit_date(strategy, underlying)
    if not exited or trading_days <= 0:
        return False
    cursor = exited
    elapsed = 0
    today = today or date.today()
    while cursor < today:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            elapsed += 1
    return elapsed < trading_days


def signal_bar_already_submitted(strategy, underlying, signal_date):
    marker = f"signal_date={signal_date}"
    return any(
        row.get("event") == "ORDER_SUBMITTED"
        and row.get("order_side") == "buy"
        and row.get("strategy") == strategy
        and row.get("underlying") == underlying
        and marker in row.get("details", "")
        for row in read_events()
    )


def get_strategy_open_lots():
    """Return net filled quantities and cost basis for each strategy contract."""
    lots = {}
    for row in read_events():
        if row.get("event") == "POSITION_MISSING":
            key = (row.get("strategy", ""), row.get("underlying", ""), row.get("option_symbol", ""))
            if key in lots:
                lots[key]["unresolved"] = True
            continue
        if row.get("event") == "POSITION_RECONCILED":
            key = (row.get("strategy", ""), row.get("underlying", ""), row.get("option_symbol", ""))
            if key in lots:
                lots[key]["unresolved"] = False
            continue
        if row.get("event") not in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}:
            continue
        strategy = row.get("strategy", "")
        symbol = row.get("option_symbol", "")
        if not strategy or not symbol:
            continue
        key = (strategy, row.get("underlying", ""), symbol)
        bucket = lots.setdefault(key, {
            "qty": 0.0, "cost": 0.0, "underlying_cost": 0.0, "opened_at": ""
        })
        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        if row.get("order_side") == "buy":
            if bucket["qty"] <= 0:
                bucket["opened_at"] = row.get("timestamp", "")
            bucket["cost"] += qty * price
            bucket["underlying_cost"] += qty * float(row.get("underlying_price") or 0)
            bucket["qty"] += qty
        elif row.get("order_side") == "sell" and bucket["qty"] > 0:
            average = bucket["cost"] / bucket["qty"]
            underlying_average = bucket["underlying_cost"] / bucket["qty"]
            closed_qty = min(qty, bucket["qty"])
            bucket["qty"] -= closed_qty
            bucket["cost"] -= closed_qty * average
            bucket["underlying_cost"] -= closed_qty * underlying_average
            if bucket["qty"] <= 0:
                bucket["opened_at"] = ""
    return {key: value for key, value in lots.items() if value["qty"] > 0}


def get_underlying_low_water_marks():
    """Rebuild open-lot stock-price lows from the durable event ledger."""
    positions = {}
    low_water_marks = {}
    for row in read_events():
        strategy = row.get("strategy", "")
        underlying = row.get("underlying", "")
        symbol = row.get("option_symbol", "")
        if not strategy or not underlying or not symbol:
            continue
        key = (strategy, underlying, symbol)
        if row.get("event") == "POSITION_MISSING":
            # A missing broker position is not evidence of a completed exit.
            continue
        if row.get("event") in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}:
            qty = float(row.get("qty") or 0)
            if row.get("order_side") == "buy":
                if positions.get(key, 0) <= 0:
                    low_water_marks.pop(key, None)
                positions[key] = positions.get(key, 0) + qty
            elif row.get("order_side") == "sell":
                positions[key] = max(positions.get(key, 0) - qty, 0)
                if positions[key] <= 0:
                    low_water_marks.pop(key, None)
        if positions.get(key, 0) <= 0:
            continue
        price = float(row.get("underlying_price") or 0)
        if price > 0 and row.get("event") in {"ORDER_FILL", "RISK_SNAPSHOT"}:
            low_water_marks[key] = min(low_water_marks.get(key, price), price)
    return low_water_marks


def get_submitted_orders():
    """Keep reservations until confirmed terminal status, including partial fills."""
    submitted, fills, filled_cost, terminal = {}, {}, {}, set()
    for row in read_events():
        order_id = row.get("order_id", "")
        if not order_id:
            continue
        if row.get("event") == "ORDER_SUBMITTED":
            submitted[order_id] = row
        elif row.get("event") in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}:
            qty = float(row.get("qty") or 0)
            fills[order_id] = fills.get(order_id, 0) + qty
            filled_cost[order_id] = filled_cost.get(order_id, 0) + qty * float(row.get("price") or 0)
        elif row.get("event") == "ORDER_TERMINAL" and row.get("order_status") != "cancel_requested":
            terminal.add(order_id)
    pending = {}
    for order_id, row in submitted.items():
        quantity = float(row.get("qty") or 0)
        filled = fills.get(order_id, 0)
        if order_id not in terminal and filled < quantity:
            pending[order_id] = dict(row, remaining_qty=quantity-filled,
                                    recorded_fill_qty=filled,
                                    recorded_fill_cost=filled_cost.get(order_id, 0))
    return pending


def get_owned_option_symbols():
    """Current confirmed local inventory, never historical account ownership."""
    return {key[2] for key in get_strategy_open_lots()}


def summarize_results():
    """Aggregate actual fills into independent strategy P/L buckets."""
    results = {"by_strategy": {}, "by_contract": {}, "by_underlying": {}}
    inventory = {}
    latest_prices = {}
    for row in read_events():
        symbol = row.get("option_symbol", "")
        if row.get("event") == "POSITION_SNAPSHOT" and symbol:
            latest_prices[symbol] = float(row.get("price") or 0)
        if row.get("event") == "POSITION_MISSING":
            key = (row.get("strategy", ""), row.get("underlying", ""), symbol)
            continue
        if row.get("event") not in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}:
            continue
        strategy = row.get("strategy", "")
        underlying = row.get("underlying", "")
        key = (strategy, underlying, symbol)
        lot = inventory.setdefault(key, {"qty": 0.0, "cost": 0.0})
        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        if row.get("order_side") == "buy":
            lot["qty"] += qty
            lot["cost"] += qty * price
            continue
        if row.get("order_side") != "sell" or lot["qty"] <= 0:
            continue
        closed_qty = min(qty, lot["qty"])
        average = lot["cost"] / lot["qty"]
        pnl = (price - average) * closed_qty * 100
        lot["qty"] -= closed_qty
        lot["cost"] -= average * closed_qty
        for group, group_key in (
            ("by_strategy", strategy), ("by_contract", symbol), ("by_underlying", underlying)
        ):
            bucket = results[group].setdefault(
                group_key, {"realized_pnl": 0.0, "unrealized_pnl": 0.0}
            )
            bucket["realized_pnl"] += pnl

    for (strategy, underlying, symbol), lot in inventory.items():
        if lot["qty"] <= 0 or symbol not in latest_prices:
            continue
        pnl = (latest_prices[symbol] * lot["qty"] - lot["cost"]) * 100
        for group, group_key in (
            ("by_strategy", strategy), ("by_contract", symbol), ("by_underlying", underlying)
        ):
            bucket = results[group].setdefault(
                group_key, {"realized_pnl": 0.0, "unrealized_pnl": 0.0}
            )
            bucket["unrealized_pnl"] += pnl
    return results


def summarize_performance_since(start_date, current_prices=None, strategy=None):
    """Return bot-only fill performance from a fixed date onward.

    Allocation return uses the virtual starting portfolio as its denominator. Fills
    before the cutoff are deliberately excluded so earlier/shared-bot activity
    cannot leak into this bot's reported performance period.
    """
    cutoff = (
        start_date if isinstance(start_date, date)
        else date.fromisoformat(str(start_date))
    )
    current_prices = current_prices or {}
    inventory = {}
    deployed_premium = 0.0
    realized_pnl = 0.0

    for row in read_events():
        try:
            event_date = datetime.fromisoformat(
                row.get("timestamp", "").replace("Z", "+00:00")
            ).date()
        except (TypeError, ValueError):
            continue
        if event_date < cutoff:
            continue

        symbol = row.get("option_symbol", "")
        key = (row.get("strategy", ""), row.get("underlying", ""), symbol)
        if row.get("event") == "POSITION_MISSING":
            continue
        if row.get("event") not in {"ORDER_FILL", "EXPIRATION_CONFIRMED"} or not symbol:
            continue
        if strategy is not None and row.get("strategy", "") != strategy:
            continue

        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        lot = inventory.setdefault(key, {"qty": 0.0, "cost": 0.0})
        if row.get("order_side") == "buy":
            premium = qty * price * 100
            lot["qty"] += qty
            lot["cost"] += premium
            deployed_premium += premium
        elif row.get("order_side") == "sell" and lot["qty"] > 0:
            closed_qty = min(qty, lot["qty"])
            average_cost = lot["cost"] / lot["qty"]
            realized_pnl += closed_qty * price * 100 - closed_qty * average_cost
            lot["qty"] -= closed_qty
            lot["cost"] -= closed_qty * average_cost

    unrealized_pnl = 0.0
    positions_value = 0.0
    open_positions = 0
    for (_, _, symbol), lot in inventory.items():
        current = current_prices.get(symbol)
        if lot["qty"] <= 0:
            continue
        open_positions += 1
        if current is not None:
            market_value = lot["qty"] * float(current) * 100
            positions_value += market_value
            unrealized_pnl += market_value - lot["cost"]

    total_pnl = realized_pnl + unrealized_pnl
    return_pct = (
        total_pnl / VIRTUAL_STARTING_CAPITAL * 100
    )
    return {
        "start_date": cutoff.isoformat(),
        "deployed_premium": deployed_premium,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "return_pct": return_pct,
        "starting_virtual_capital": VIRTUAL_STARTING_CAPITAL,
        "ending_virtual_capital": VIRTUAL_STARTING_CAPITAL + total_pnl,
        "return_on_capital_employed_percent": total_pnl / deployed_premium * 100 if deployed_premium else 0.0,
        "open_positions": open_positions,
        "positions_value": positions_value,
    }


def build_strategy_report(strategy_names=()):
    """Build fill-based paper performance and open-position details by strategy."""
    events = read_events()
    latest_prices = {}
    pending = get_submitted_orders()
    report = {
        name: {
            "completed_trades": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "open_positions": [],
            "pending_orders": 0,
        }
        for name in strategy_names
    }
    inventory = {}

    for row in events:
        symbol = row.get("option_symbol", "")
        if row.get("event") == "POSITION_SNAPSHOT" and symbol:
            latest_prices[symbol] = float(row.get("price") or 0)
        if row.get("event") == "POSITION_MISSING":
            key = (row.get("strategy", ""), row.get("underlying", ""), symbol)
            continue
        if row.get("event") not in {"ORDER_FILL", "EXPIRATION_CONFIRMED"}:
            continue
        strategy = row.get("strategy", "")
        if not strategy:
            continue
        stats = report.setdefault(strategy, {
            "completed_trades": 0, "wins": 0, "losses": 0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "open_positions": [], "pending_orders": 0,
        })
        key = (strategy, row.get("underlying", ""), symbol)
        lot = inventory.setdefault(key, {"qty": 0.0, "cost": 0.0})
        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        if row.get("order_side") == "buy":
            lot["qty"] += qty
            lot["cost"] += qty * price
        elif row.get("order_side") == "sell" and lot["qty"] > 0:
            closed_qty = min(qty, lot["qty"])
            average = lot["cost"] / lot["qty"]
            pnl = (price - average) * closed_qty * 100
            lot["qty"] -= closed_qty
            lot["cost"] -= average * closed_qty
            stats["completed_trades"] += 1
            stats["realized_pnl"] += pnl
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1

    for (strategy, underlying, symbol), lot in inventory.items():
        if lot["qty"] <= 0:
            continue
        current = latest_prices.get(symbol)
        average = lot["cost"] / lot["qty"]
        unrealized = None
        if current is not None:
            unrealized = (current - average) * lot["qty"] * 100
            report[strategy]["unrealized_pnl"] += unrealized
        report[strategy]["open_positions"].append({
            "underlying": underlying,
            "option_symbol": symbol,
            "qty": lot["qty"],
            "average_entry_price": average,
            "current_price": current,
            "unrealized_pnl": unrealized,
        })

    for row in pending.values():
        strategy = row.get("strategy", "")
        if strategy:
            report.setdefault(strategy, {
                "completed_trades": 0, "wins": 0, "losses": 0,
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "open_positions": [], "pending_orders": 0,
            })["pending_orders"] += 1
    # Preserve the existing display while counting full round trips consistently.
    from research import portfolio_report
    for name, stats in report.items():
        metrics = portfolio_report([row for row in events if row.get("strategy") in (name, "")])
        stats.update(completed_trades=metrics["trade_count"], wins=metrics["winning_trades"],
                     losses=metrics["losing_trades"], realized_pnl=metrics["realized_pnl"],
                     unrealized_pnl=metrics["unrealized_pnl"])
    return report


def get_latest_entry_price(underlying, option_symbol):
    path = Path(ANALYTICS_FILE)
    if not path.exists():
        return None
    _ensure_schema(path)

    latest_price = None

    with path.open("r", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if (
                row.get("event") == "BUY_SUBMITTED"
                and row.get("underlying") == underlying
                and row.get("option_symbol") == option_symbol
            ):
                try:
                    latest_price = float(row.get("price") or 0)
                except ValueError:
                    latest_price = None

    return latest_price
