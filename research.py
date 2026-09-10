"""CSV rejection research and fill-based long-put portfolio statistics.

No broker equity is used. Variants share one virtual allocation. Missing broker
positions are unresolved, never fabricated exits or assumed worthless expirations.
"""
import csv
import json
import math
import re
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from config import (STRATEGY_ID, VIRTUAL_STARTING_CAPITAL, REJECTED_TRADES_FILE,
                    RESEARCH_REPORT_FILE)

PUT_RE = re.compile(r'^([A-Z.]+)(\d{6})P(\d{8})$')
REJECTION_FIELDS = ['timestamp', 'strategy', 'variant', 'underlying',
    'contract_symbol', 'call_or_put', 'long_or_short', 'strike', 'expiration',
    'DTE', 'underlying_price', 'bid', 'ask', 'mid', 'spread_dollars',
    'spread_percent', 'option_premium', 'required_capital',
    'virtual_capital_available', 'rejection_reason', 'signal_score',
    'market_regime', 'signal_date', 'details']
_context = ContextVar('qualified_opportunity', default=None)


def details_dict(value):
    return dict(part.split('=', 1) for part in (value or '').split(';') if '=' in part)


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def put_metadata(symbol, timestamp=None):
    match = PUT_RE.fullmatch(symbol or '')
    if not match:
        return {}
    underlying, expiry, strike = match.groups()
    expiration = datetime.strptime(expiry, '%y%m%d').date()
    day = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).date() if timestamp else datetime.now(timezone.utc).date()
    return dict(underlying=underlying, strike=int(strike) / 1000,
                expiration=expiration.isoformat(), DTE=(expiration - day).days)


def is_own_event(row):
    # Old ledgers used variant names without a bot identifier. Only recognize
    # those local long-put rows; explicit foreign identifiers always win.
    if row.get('bot_strategy') not in (None, '', STRATEGY_ID):
        return False
    if row.get('strategy', '') not in ('', 'regular', 'max_100', STRATEGY_ID):
        return False
    symbol = row.get('option_symbol', '')
    return not symbol or bool(PUT_RE.fullmatch(symbol))


@contextmanager
def qualified_opportunity(variant, underlying, signal_date='', market_regime='bearish'):
    token = _context.set(dict(variant=variant, underlying=underlying,
                             signal_date=signal_date, market_regime=market_regime))
    try:
        yield
    finally:
        _context.reset(token)


def has_opportunity():
    return _context.get() is not None


def update_opportunity(**values):
    if _context.get() is not None:
        _context.get().update(values)


def rejection_reason(reason):
    if reason.isupper():
        return reason
    if reason in ('max_premium_per_trade',): return 'PREMIUM_OVER_LIMIT'
    if reason in ('open_interest_filter', 'option_volume_filter'): return 'INSUFFICIENT_LIQUIDITY'
    if reason == 'spread_too_wide': return 'SPREAD_TOO_WIDE'
    if reason in ('duplicate_contract', 'multiple_underlying_contracts', 'already_holding', 'signal_bar_already_traded'):
        return 'DUPLICATE_POSITION'
    if reason in ('max_positions', 'max_total_option_premium') or reason.startswith('correlation_group_'):
        return 'MAX_STRATEGY_EXPOSURE_REACHED'
    if reason in ('no_contracts', 'contract_quality_filters'): return 'NO_VALID_CONTRACT'
    return 'OTHER'


def record_rejection(reason, *, path=None, **values):
    row = dict(_context.get() or {})
    row.update({key: value for key, value in values.items() if value not in ('', None)})
    symbol = row.get('contract_symbol', '')
    stamp = row.get('timestamp') or datetime.now(timezone.utc).isoformat(timespec='seconds')
    row = {**put_metadata(symbol, stamp), **row}
    row.update(timestamp=stamp, strategy=STRATEGY_ID, call_or_put='put',
               long_or_short='long', rejection_reason=rejection_reason(reason))
    bid, ask = number(row.get('bid'), None), number(row.get('ask'), None)
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        row.update(mid=mid, spread_dollars=ask - bid, spread_percent=(ask-bid)/mid*100)
        row.setdefault('option_premium', mid * 100)
        row.setdefault('required_capital', row['option_premium'])
    target = Path(path or REJECTED_TRADES_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = not target.exists() or target.stat().st_size == 0
    with target.open('a', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=REJECTION_FIELDS, extrasaction='ignore')
        if header: writer.writeheader()
        writer.writerow(row)
    return row


def observe_event(event, **row):
    """Capture rejected qualified opportunities without counting ordinary scans."""
    if _context.get() is None:
        return
    values = details_dict(row.get('details', ''))
    if event == 'CONTRACT_SELECTED':
        update_opportunity(contract_symbol=row.get('option_symbol', ''),
                           **{k: values[k] for k in ('bid', 'ask', 'underlying_price') if k in values})
    if event in ('SKIP', 'ORDER_FAILED'):
        record_rejection(row.get('reason', 'OTHER'),
                         contract_symbol=row.get('option_symbol', ''),
                         details=row.get('details') or row.get('reason', ''))


def portfolio_report(events, starting_capital=VIRTUAL_STARTING_CAPITAL, current_prices=None):
    """Round-trip stats from confirmed fills, with sampled mark-to-market drawdown.

    ROC = total P/L / cumulative entry premium. Allocation return instead uses
    starting capital. Unmarked lots carry cost until a mark arrives and are flagged.
    """
    inventory, marks, trades, entry_costs, dtes, spreads = {}, {}, [], [], [], []
    submitted = {}
    realized = premium_paid = peak_employed = 0.0
    peak_equity = starting_capital
    max_dd = max_dd_pct = 0.0
    missing = set()
    observed_marks = set()
    def equity():
        return starting_capital + realized + sum(
            lot['qty'] * marks.get(key[2], lot['cost'] / lot['qty']) * 100 - lot['cost'] * 100
            for key, lot in inventory.items() if lot['qty'] > 0)
    for row in events:
        if not is_own_event(row): continue
        kind, symbol = row.get('event'), row.get('option_symbol', '')
        key = (row.get('strategy', ''), row.get('underlying', ''), symbol)
        if kind == 'ORDER_SUBMITTED': submitted[row.get('order_id')] = row
        if kind in ('POSITION_SNAPSHOT', 'RISK_SNAPSHOT') and symbol and row.get('price') not in ('', None):
            marks[symbol] = number(row['price'])
            observed_marks.add(symbol)
        if kind == 'POSITION_MISSING': missing.add(key)
        if kind == 'POSITION_RECONCILED': missing.discard(key)
        if kind not in ('ORDER_FILL', 'EXPIRATION_CONFIRMED'):
            value = equity(); peak_equity = max(peak_equity, value)
            max_dd = max(max_dd, peak_equity - value)
            max_dd_pct = max(max_dd_pct, (peak_equity-value)/peak_equity*100 if peak_equity else 0)
            continue
        qty, price = number(row.get('qty')), number(row.get('price'))
        if qty <= 0: continue
        lot = inventory.setdefault(key, dict(qty=0., cost=0., entry_cost=0., pnl=0., opened_at=''))
        if row.get('order_side') == 'buy':
            if lot['qty'] == 0:
                lot.update(entry_cost=0., pnl=0., opened_at=row.get('timestamp', ''))
                entry_costs.append(0.)
                lot['entry_index'] = len(entry_costs)-1
                meta = put_metadata(symbol, row.get('timestamp') or None)
                if meta: dtes.append(meta['DTE'])
                info = details_dict(submitted.get(row.get('order_id'), {}).get('details', ''))
                spread = number(info.get('spread_pct'), None)
                if spread is not None: spreads.append(spread * 100)
            lot['qty'] += qty; lot['cost'] += qty * price
            lot['entry_cost'] += qty * price * 100
            entry_costs[lot['entry_index']] += qty * price * 100
            premium_paid += qty * price * 100
            marks[symbol] = price
        elif row.get('order_side') == 'sell' and lot['qty'] > 0:
            closed = min(qty, lot['qty']); average = lot['cost']/lot['qty']
            pnl = closed * (price-average)*100
            realized += pnl; lot['pnl'] += pnl
            lot['qty'] -= closed; lot['cost'] -= closed * average
            marks[symbol] = price
            if lot['qty'] <= 1e-9:
                lot['qty'] = 0; missing.discard(key)
                hold = None
                try:
                    hold = (datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')) -
                            datetime.fromisoformat(lot['opened_at'].replace('Z', '+00:00'))).total_seconds()/86400
                except (KeyError, TypeError, ValueError): pass
                trades.append(dict(pnl=lot['pnl'], capital=lot['entry_cost'], hold=hold,
                                   worthless=kind == 'EXPIRATION_CONFIRMED' and price == 0))
        employed = sum(item['cost']*100 for item in inventory.values())
        peak_employed = max(peak_employed, employed)
        value=equity(); peak_equity=max(peak_equity,value)
        max_dd=max(max_dd,peak_equity-value)
        max_dd_pct=max(max_dd_pct,(peak_equity-value)/peak_equity*100 if peak_equity else 0)
    marks.update(current_prices or {})
    open_lots = {key: lot for key,lot in inventory.items() if lot['qty'] > 0}
    employed = sum(lot['cost']*100 for lot in open_lots.values())
    unmarked = sum(key[2] not in observed_marks and key[2] not in (current_prices or {}) for key in open_lots)
    # Entry fills establish a carrying mark; distinguish later valuation coverage.
    total = equity()-starting_capital
    final_equity=starting_capital+total; peak_equity=max(peak_equity,final_equity)
    max_dd=max(max_dd,peak_equity-final_equity)
    max_dd_pct=max(max_dd_pct,(peak_equity-final_equity)/peak_equity*100 if peak_equity else 0)
    wins=[t['pnl'] for t in trades if t['pnl'] > 0]
    losses=[t['pnl'] for t in trades if t['pnl'] < 0]
    holds=[t['hold'] for t in trades if t['hold'] is not None]
    def avg(values): return sum(values)/len(values) if values else None
    return dict(strategy=STRATEGY_ID, starting_virtual_capital=starting_capital,
        ending_virtual_capital=final_equity, realized_pnl=realized,
        unrealized_pnl=total-realized, total_pnl=total, total_return_percent=total/starting_capital*100,
        return_on_capital_employed_percent=total/premium_paid*100 if premium_paid else 0.,
        average_capital_employed_per_trade=avg(entry_costs), maximum_capital_employed=peak_employed,
        current_capital_employed=employed, exposure_percent=employed/starting_capital*100,
        virtual_cash=starting_capital+realized-employed,
        trade_count=len(trades), winning_trades=len(wins), losing_trades=len(losses), entry_trade_count=len(entry_costs), open_positions=len(open_lots),
        win_rate_percent=len(wins)/len(trades)*100 if trades else 0.,
        average_winner=avg(wins), average_loser=avg(losses),
        expectancy=avg([t['pnl'] for t in trades]),
        profit_factor=sum(wins)/abs(sum(losses)) if losses else None,
        profit_factor_status='defined' if losses else ('no_losses' if wins else 'no_closed_trades'),
        max_drawdown=max_dd, max_drawdown_percent=max_dd_pct,
        average_hold_days=avg(holds), largest_winner=max(wins) if wins else None,
        largest_loser=min(losses) if losses else None, premium_paid=premium_paid,
        premium_lost=abs(sum(losses)), option_return_percent=total/premium_paid*100 if premium_paid else 0.,
        average_option_premium=avg(entry_costs), average_DTE=avg(dtes), average_spread_percent=avg(spreads),
        contracts_expired_worthless=sum(t['worthless'] for t in trades),
        unresolved_positions=len(missing), unmarked_positions=unmarked,
        valuation_basis='latest recorded marks; entry cost fallback; missing positions unresolved',
        drawdown_basis='sampled ledger marks and fills')


def capital_snapshot():
    from analytics import read_events, get_submitted_orders
    report = portfolio_report(read_events())
    pending = get_submitted_orders().values()
    reserved = sum(number(r.get('remaining_qty', r.get('qty'))) * number(r.get('price')) * 100
                   for r in pending if r.get('order_side') == 'buy')
    return dict(available=max(0., min(VIRTUAL_STARTING_CAPITAL, report['virtual_cash'])-reserved),
                employed=report['current_capital_employed']+reserved,
                pending_premium=reserved, unresolved=report['unresolved_positions'])


def export_research_report(current_prices=None):
    from analytics import read_events
    report = portfolio_report(read_events(), current_prices=current_prices)
    report['capital'] = capital_snapshot()
    path=Path(REJECTED_TRADES_FILE)
    counts=Counter()
    if path.exists():
        with path.open(newline='') as stream:
            counts.update(row['rejection_reason'] for row in csv.DictReader(stream))
    report['rejected_opportunities']=dict(counts)
    report['qualified_signals'] = sum(row.get('event') == 'SIGNAL_QUALIFIED' for row in read_events())
    report['premium_only_rejections'] = counts.get('PREMIUM_OVER_LIMIT', 0)
    target=Path(RESEARCH_REPORT_FILE);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    return report
