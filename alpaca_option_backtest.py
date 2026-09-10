"""Reprice signal trades with Alpaca historical option bars.

Alpaca option history starts in February 2024. This module deliberately does not
fall back to theoretical prices: a candidate is skipped when no real contract bar
is available near the requested entry or exit.
"""

from datetime import datetime, time, timedelta, timezone
import math

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from config import API_KEY, SECRET_KEY, ALPACA_PAPER, MAX_DTE, MIN_DTE, MAX_OPTION_PREMIUM_PER_TRADE


def _utc_start(iso_date):
    day = datetime.fromisoformat(iso_date).date()
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


class AlpacaOptionRepricer:
    def __init__(self):
        self.trading = TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)
        self.data = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

    def _contracts(self, underlying, entry_date, target_strike):
        entry_day = datetime.fromisoformat(entry_date).date()
        lower_expiration = entry_day + timedelta(days=MIN_DTE)
        upper_expiration = entry_day + timedelta(days=MAX_DTE)
        lower_strike = max(target_strike * 0.90, 0.5)
        upper_strike = target_strike * 1.10
        contracts = []

        for status in (AssetStatus.INACTIVE, AssetStatus.ACTIVE):
            page_token = None
            while True:
                response = self.trading.get_option_contracts(
                    GetOptionContractsRequest(
                        underlying_symbols=[underlying],
                        status=status,
                        expiration_date_gte=lower_expiration,
                        expiration_date_lte=upper_expiration,
                        type=ContractType.PUT,
                        strike_price_gte=f"{lower_strike:.2f}",
                        strike_price_lte=f"{upper_strike:.2f}",
                        limit=1000,
                        page_token=page_token,
                    )
                )
                contracts.extend(response.option_contracts)
                page_token = response.next_page_token
                if not page_token:
                    break
        return contracts

    def _bars(self, symbols, start, end):
        if not symbols:
            return {}
        response = self.data.get_option_bars(
            OptionBarsRequest(
                symbol_or_symbols=symbols,
                start=start,
                end=end,
                timeframe=TimeFrame.Day,
            )
        )
        return response.data

    def reprice(self, candidate, max_entry_premium=MAX_OPTION_PREMIUM_PER_TRADE):
        self.last_rejection = None
        underlying = candidate["symbol"]
        entry_underlying = float(candidate["entry_underlying_price"])
        from backtester import estimate_strike_for_delta
        from config import TARGET_DELTA, BACKTEST_ENTRY_DTE
        target_strike = estimate_strike_for_delta(entry_underlying, TARGET_DELTA, BACKTEST_ENTRY_DTE)
        contracts = self._contracts(
            underlying, candidate["entry_date"], target_strike
        )
        contracts.sort(
            key=lambda item: (
                abs(float(item.strike_price) - target_strike),
                abs((item.expiration_date - datetime.fromisoformat(
                    candidate["entry_date"]
                ).date()).days - 75),
            )
        )

        # Rank first, independently of price. Never replace a preferred contract
        # with an inferior affordable strike. Historical delta is an estimate;
        # daily bars do not contain historical quotes, Greeks or open interest.
        if not contracts:
            self.last_rejection = dict(candidate, rejection_reason="NO_VALID_CONTRACT",
                                       details="no_historical_contract")
            return None
        contract = contracts[0]
        entry_start = _utc_start(candidate["entry_date"])
        history = self._bars([contract.symbol], entry_start,
            _utc_start(candidate["exit_date"])+timedelta(days=1)).get(contract.symbol, [])
        entry_bars = [bar for bar in history if bar.timestamp.date() == entry_start.date()]
        exit_day = _utc_start(candidate["exit_date"]).date()
        exit_bars = [bar for bar in history if bar.timestamp.date() == exit_day]
        if not entry_bars or not exit_bars:
            self.last_rejection = dict(candidate, option_symbol=contract.symbol,
                rejection_reason="OTHER", details="missing_exact_entry_or_exit_bar")
            return None
        entry_price, exit_price = float(entry_bars[0].close), float(exit_bars[-1].close)
        if not math.isfinite(entry_price) or not math.isfinite(exit_price) or entry_price <= 0 or exit_price < 0:
            self.last_rejection = dict(candidate, option_symbol=contract.symbol,
                rejection_reason="OTHER", details="invalid_historical_bar_price")
            return None
        if max_entry_premium is not None and entry_price*100 > max_entry_premium:
            self.last_rejection = dict(candidate, option_symbol=contract.symbol,
                rejection_reason="PREMIUM_OVER_LIMIT", required_capital=entry_price*100,
                details="preferred_contract_over_limit;no_substitution")
            return None
        pnl = (exit_price - entry_price) * 100
        return {
            **candidate,
            "capital_employed": entry_price * 100,
            "entry_dte": (contract.expiration_date-entry_start.date()).days,
            "exit_dte": (contract.expiration_date-exit_day).days,
            "estimated_strike": float(contract.strike_price),
            "estimated_entry_delta": "", "estimated_exit_delta": "",
            "contracts": 1,
            "symbol": underlying,
            "option_symbol": contract.symbol,
            "entry_date": candidate["entry_date"],
            "exit_date": candidate["exit_date"],
            "estimated_option_entry_price": entry_price,
            "estimated_option_exit_price": exit_price,
            "pnl_dollars": round(pnl, 2),
            "pnl_percent": round((exit_price - entry_price) / entry_price * 100, 2),
            "exit_reason": candidate["exit_reason"],
        }


def reprice_candidates(candidates, max_entry_premium=MAX_OPTION_PREMIUM_PER_TRADE, max_candidates=None, rejected=None):
    repricer = AlpacaOptionRepricer()
    selected = candidates[-max_candidates:] if max_candidates else candidates
    trades = []
    for index, candidate in enumerate(selected, start=1):
        print(
            f"Repricing {index}/{len(selected)}: {candidate['symbol']} "
            f"{candidate['entry_date']}"
        )
        trade = repricer.reprice(candidate, max_entry_premium=max_entry_premium)
        if trade:
            trades.append(trade)
        elif rejected is not None and repricer.last_rejection:
            rejected.append(repricer.last_rejection)
    return trades
