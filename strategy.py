import ta
import pandas as pd
from datetime import date, datetime, timedelta
from time import monotonic, sleep
from requests.exceptions import RequestException
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot_logger import bot_log


_daily_close_cache = {}
_stock_data_client = None
_DAILY_CLOSE_CACHE_SECONDS = 240
_DAILY_CLOSE_MAX_STALE_SECONDS = 24 * 60 * 60
_ALPACA_DOWNLOAD_ATTEMPTS = 3


def configure_daily_data_client(client):
    """Provide the authenticated Alpaca client used for daily signal bars."""
    global _stock_data_client
    _stock_data_client = client


def _download_daily_history(symbol):
    """Retry Alpaca daily bars before declaring signal data unavailable."""
    if _stock_data_client is None:
        bot_log(f"Alpaca daily-history client is not configured for {symbol}.")
        return None

    for attempt in range(1, _ALPACA_DOWNLOAD_ATTEMPTS + 1):
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now() - timedelta(days=4 * 365),
                feed=DataFeed.IEX,
            )
            response = _stock_data_client.get_stock_bars(request)
            bars = response.data.get(symbol, [])
            if bars:
                return pd.DataFrame(
                    {"Close": [float(bar.close) for bar in bars]},
                    index=pd.DatetimeIndex([bar.timestamp for bar in bars]),
                )
        except Exception as exc:
            bot_log(
                f"Alpaca daily-history attempt {attempt}/{_ALPACA_DOWNLOAD_ATTEMPTS} "
                f"failed for {symbol}: {exc}"
            )
        if attempt < _ALPACA_DOWNLOAD_ATTEMPTS:
            sleep(attempt)
    return None


def wait_for_market_open(trading_client):
    """Wait for a confirmed open clock, retrying broker and transport failures."""
    import time

    while True:
        try:
            clock = trading_client.get_clock()
        except (APIError, RequestException) as e:
            bot_log(f"Could not get market clock from Alpaca: {e}. Retrying in 60 seconds.")
            time.sleep(60)
            continue

        if clock.is_open:
            bot_log("Market is open.")
            break

        bot_log("Market closed. Waiting...")
        time.sleep(60)


def _completed_daily_close(symbol):
    cached = _daily_close_cache.get(symbol)
    if cached and monotonic() - cached[0] < _DAILY_CLOSE_CACHE_SECONDS:
        return cached[1]
    data = _download_daily_history(symbol)
    if data is None or data.empty:
        if cached and monotonic() - cached[0] <= _DAILY_CLOSE_MAX_STALE_SECONDS:
            bot_log(f"Using last successful daily history for {symbol}; Alpaca refresh failed.")
            return cached[1]
        return None
    close = data["Close"].squeeze().dropna()
    if not close.empty and close.index[-1].date() >= date.today():
        close = close.iloc[:-1]
    result = close if not close.empty else None
    _daily_close_cache[symbol] = (monotonic(), result)
    return result


def latest_completed_bar_date(symbol):
    """Return the date of the newest completed daily bar, if available."""
    close = _completed_daily_close(symbol)
    if close is None or close.empty:
        return None
    return close.index[-1].date()


def _daily_indicators(close, ma_short, ma_long, macd_fast, macd_slow, macd_signal):
    macd = ta.trend.MACD(
        close=close,
        window_fast=macd_fast,
        window_slow=macd_slow,
        window_sign=macd_signal,
    )
    return {
        "ma_short": close.rolling(ma_short).mean(),
        "ma_long": close.rolling(ma_long).mean(),
        "ema_10": close.ewm(span=10, adjust=False).mean(),
        "ema_20": close.ewm(span=20, adjust=False).mean(),
        "rsi": ta.momentum.RSIIndicator(close, window=14).rsi(),
        "macd": macd.macd(),
        "macd_signal": macd.macd_signal(),
        "macd_hist": macd.macd_diff(),
    }


def _bearish_at(close, indicators, index, signal):
    if index < 1:
        return False
    values = [
        close.iloc[index], indicators["ma_short"].iloc[index],
        indicators["ma_short"].iloc[index - 1], indicators["ma_long"].iloc[index],
        indicators["macd"].iloc[index], indicators["macd_signal"].iloc[index],
        indicators["macd_hist"].iloc[index],
    ]
    if signal == "daily_swing":
        values.extend([
            close.iloc[index - 1], indicators["ema_10"].iloc[index],
            indicators["ema_20"].iloc[index], indicators["ema_20"].iloc[index - 1],
            indicators["rsi"].iloc[index],
        ])
    if any(value != value for value in values):
        return False

    latest, ma_short, previous_ma_short, ma_long, macd, macd_signal, macd_hist = values[:7]
    if signal == "daily_swing":
        previous, ema_10, ema_20, previous_ema_20, rsi = values[7:]
        return (
            latest < ma_long and ma_short < ma_long
            and previous >= previous_ema_20 and latest < ema_20
            and latest < ema_10 and 35 <= rsi <= 55 and macd_hist < 0
        )
    return (
        latest < ma_short < ma_long
        and ma_short < previous_ma_short
        and macd < macd_signal and macd_hist < 0
    )


def get_bearish_signal_state(
    symbol, ma_short, ma_long, macd_fast, macd_slow, macd_signal,
    signal="daily_trend",
):
    """Return the completed-bar signal state and whether it just turned bearish."""
    try:
        close = _completed_daily_close(symbol)
        if close is None or len(close) < ma_long + 5:
            return {
                "bearish": False, "new_signal": False, "signal_date": "",
                "data_available": close is not None,
            }
        indicators = _daily_indicators(
            close, ma_short, ma_long, macd_fast, macd_slow, macd_signal
        )
        current = _bearish_at(close, indicators, len(close) - 1, signal)
        previous = _bearish_at(close, indicators, len(close) - 2, signal)
        return {
            "bearish": current,
            "new_signal": current and not previous,
            "signal_date": close.index[-1].date().isoformat(),
            "data_available": True,
        }
    except Exception as exc:
        bot_log(f"Signal-state error for {symbol}: {exc}")
        return {
            "bearish": False, "new_signal": False, "signal_date": "",
            "data_available": False,
        }


def is_bearish_setup(
    symbol, ma_short, ma_long, macd_fast, macd_slow, macd_signal,
    signal="daily_trend",
):
    try:
        close = _completed_daily_close(symbol)
        if close is None:
            return False

        if len(close) < ma_long + 5:
            return False

        indicators = _daily_indicators(
            close, ma_short, ma_long, macd_fast, macd_slow, macd_signal
        )

        latest_close = float(close.iloc[-1])
        latest_ma_short = float(indicators["ma_short"].iloc[-1])
        prev_ma_short = float(indicators["ma_short"].iloc[-2])
        latest_ma_long = float(indicators["ma_long"].iloc[-1])
        latest_macd = float(indicators["macd"].iloc[-1])
        latest_macd_signal = float(indicators["macd_signal"].iloc[-1])
        latest_macd_hist = float(indicators["macd_hist"].iloc[-1])

        in_downtrend = latest_close < latest_ma_short < latest_ma_long
        ma_falling = latest_ma_short < prev_ma_short
        macd_confirmed = latest_macd < latest_macd_signal and latest_macd_hist < 0

        if signal == "daily_swing":
            previous_close = float(close.iloc[-2])
            ema_10 = float(indicators["ema_10"].iloc[-1])
            ema_20 = float(indicators["ema_20"].iloc[-1])
            previous_ema_20 = float(indicators["ema_20"].iloc[-2])
            rsi = float(indicators["rsi"].iloc[-1])
            bearish = _bearish_at(close, indicators, len(close) - 1, signal)
            bot_log(
                f"{symbol} daily_swing: close={latest_close:.2f}, EMA20={ema_20:.2f}, "
                f"RSI={rsi:.2f}, MACD hist={latest_macd_hist:.4f}, bearish={bearish}"
            )
            return bearish

        bearish = _bearish_at(close, indicators, len(close) - 1, signal)
        bot_log(
            f"{symbol} daily_trend: close={latest_close:.2f}, "
            f"MA50={latest_ma_short:.2f}, MA200={latest_ma_long:.2f}, "
            f"MACD hist={latest_macd_hist:.4f}, bearish={bearish}"
        )
        return bearish

    except Exception as e:
        bot_log(f"Strategy error for {symbol}: {e}")
        return False


def is_market_regime_bearish(symbol, short_ma, long_ma):
    try:
        close = _completed_daily_close(symbol)
        if close is None:
            return False

        if len(close) < long_ma + 5:
            return False

        short_series = close.rolling(short_ma).mean()
        long_series = close.rolling(long_ma).mean()

        latest_close = float(close.iloc[-1])
        latest_short = float(short_series.iloc[-1])
        latest_long = float(long_series.iloc[-1])

        bearish = latest_close < latest_long and latest_short < latest_long
        bot_log(
            f"Market regime {symbol}: close={latest_close:.2f}, "
            f"MA{short_ma}={latest_short:.2f}, MA{long_ma}={latest_long:.2f}, "
            f"bearish={bearish}"
        )

        return bearish

    except Exception as e:
        bot_log(f"Market regime error for {symbol}: {e}")
        return False


def is_underlying_exit_signal(
    symbol, ma_short, ma_long, macd_fast, macd_slow, macd_signal,
    signal="daily_trend",
):
    try:
        close = _completed_daily_close(symbol)
        if close is None:
            return False, "no_data"

        if len(close) < ma_long + 5:
            return False, "not_enough_data"

        indicators = _daily_indicators(
            close, ma_short, ma_long, macd_fast, macd_slow, macd_signal
        )

        latest_close = float(close.iloc[-1])
        latest_ma_short = float(indicators["ma_short"].iloc[-1])
        latest_ma_long = float(indicators["ma_long"].iloc[-1])
        latest_macd_hist = float(indicators["macd_hist"].iloc[-1])

        if signal == "daily_swing":
            latest_ema_20 = float(indicators["ema_20"].iloc[-1])
            if latest_close > latest_ema_20:
                return True, "close_above_ema_20"
            return False, ""

        if latest_close > latest_ma_long:
            return True, "close_above_long_ma"

        if latest_ma_short > latest_ma_long:
            return True, "short_ma_above_long_ma"

        if latest_macd_hist > 0:
            return True, "macd_hist_positive"

        return False, ""

    except Exception as e:
        bot_log(f"Exit signal error for {symbol}: {e}")
        return False, "exit_signal_error"
