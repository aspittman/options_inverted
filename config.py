import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY_ENV_NAMES = ("APCA_API_KEY_ID", "ALPACA_API_KEY", "API_KEY")
ALPACA_SECRET_KEY_ENV_NAMES = ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "SECRET_KEY")


def _first_env_value(names):
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()

    return None


API_KEY = _first_env_value(ALPACA_API_KEY_ENV_NAMES)
SECRET_KEY = _first_env_value(ALPACA_SECRET_KEY_ENV_NAMES)
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
BOT_PERFORMANCE_START_DATE = os.getenv("BOT_PERFORMANCE_START_DATE", "2026-09-07")


def require_alpaca_credentials():
    missing = []

    if not API_KEY:
        missing.append("API key")

    if not SECRET_KEY:
        missing.append("secret key")

    if missing:
        raise RuntimeError(
            "Missing Alpaca credentials: "
            + ", ".join(missing)
            + ". Set them in .env or your shell using "
            + f"one API key variable from {ALPACA_API_KEY_ENV_NAMES} and "
            + f"one secret key variable from {ALPACA_SECRET_KEY_ENV_NAMES}."
        )

    return API_KEY, SECRET_KEY

UNDERLYINGS = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "GOOG",
    "TSLA",
    "AMD",
    "NFLX",
    "AVGO",
    "CRM",
    "ORCL",
    "ADBE",
    "INTC",
    "QCOM",
    "MU",
    "JPM",
    "BAC",
    "GS",
    "MS",
    "C",
    "XOM",
    "CVX",
    "COP",
    "SLB",
    "UNH",
    "LLY",
    "JNJ",
    "PFE",
    "MRK",
    "COST",
    "WMT",
    "HD",
    "DIS",
    "BA",
]

DOLLARS_PER_TRADE = 100


def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    return int(os.getenv(name, str(default)))


def _env_float(name, default):
    return float(os.getenv(name, str(default)))


MAX_POSITIONS = _env_int("MAX_POSITIONS", 2)
ENABLE_NEW_ENTRIES = _env_bool("ENABLE_NEW_ENTRIES", False)
MAX_PREMIUM_PER_TRADE = _env_float("MAX_PREMIUM_PER_TRADE", 500.0)
MAX_TOTAL_OPTION_PREMIUM = _env_float("MAX_TOTAL_OPTION_PREMIUM", 1000.0)
REGULAR_MAX_PREMIUM_PER_TRADE = _env_float("REGULAR_MAX_PREMIUM_PER_TRADE", 500.0)
MAX_100_PREMIUM_PER_TRADE = _env_float("MAX_100_PREMIUM_PER_TRADE", 500.0)
UNDERLYING_TRAILING_STOP_PERCENT = _env_float(
    "UNDERLYING_TRAILING_STOP_PERCENT", 0.03
)
PAPER_STRATEGIES = (
    {
        "name": "regular",
        "signal": "daily_trend",
        "max_premium": REGULAR_MAX_PREMIUM_PER_TRADE or None,
        "underlying_trailing_stop": UNDERLYING_TRAILING_STOP_PERCENT,
        "underlying_take_profit": 0.08,
        "max_holding_days": 20,
    },
    {
        "name": "max_100",
        "signal": "daily_swing",
        "max_premium": MAX_100_PREMIUM_PER_TRADE,
        "underlying_trailing_stop": UNDERLYING_TRAILING_STOP_PERCENT,
        "underlying_take_profit": 0.06,
        "max_holding_days": 15,
    },
)
ALLOW_DUPLICATE_CONTRACTS = _env_bool("ALLOW_DUPLICATE_CONTRACTS", False)
ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING = _env_bool(
    "ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING", False
)
MAX_POSITIONS_PER_CORRELATION_GROUP = _env_int(
    "MAX_POSITIONS_PER_CORRELATION_GROUP", 1
)
CORRELATION_GROUPS = {
    "broad_index": {"SPY", "QQQ", "IWM", "DIA"},
    "technology": {
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
        "AMD", "NFLX", "AVGO", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU",
    },
    "financials": {"JPM", "BAC", "GS", "MS", "C"},
    "energy": {"XOM", "CVX", "COP", "SLB"},
    "healthcare": {"UNH", "LLY", "JNJ", "PFE", "MRK"},
    "consumer_industrial": {"COST", "WMT", "HD", "DIS", "BA"},
}


def correlation_group(symbol):
    return next(
        (name for name, symbols in CORRELATION_GROUPS.items() if symbol in symbols),
        symbol,
    )

MA_SHORT = 50
MA_LONG = 200
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

MIN_DTE = 60
MAX_DTE = 90

MIN_OPEN_INTEREST = 500
MIN_OPTION_VOLUME = 100
MAX_BID_ASK_SPREAD_PCT = 0.05
TARGET_DELTA = -0.60
DELTA_TOLERANCE = 0.10
EARNINGS_SKIP_DAYS = 1  # skip if earnings are today or tomorrow
OPTION_DATA_FEED = os.getenv("OPTION_DATA_FEED", "indicative")

ENABLE_MARKET_REGIME_FILTER = True
MARKET_REGIME_SYMBOL = "SPY"
MARKET_REGIME_SHORT_MA = 50
MARKET_REGIME_LONG_MA = 200

UNDERLYING_TAKE_PROFIT_PCT = 0.08

BACKTEST_ENTRY_DTE = 75
BACKTEST_OPTION_TIME_VALUE_PERCENT = 0.12
BACKTEST_STARTING_CASH = _env_float("BACKTEST_STARTING_CASH", 2500.0)
OPTION_STOP_LOSS_PERCENT = _env_float("OPTION_STOP_LOSS_PERCENT", 0.30)
OPTION_TRAILING_STOP_PERCENT = _env_float("OPTION_TRAILING_STOP_PERCENT", 0.0)
OPTION_TAKE_PROFIT_PERCENT = 1.00
EXIT_DTE = _env_int("EXIT_DTE", 30)
MAX_HOLDING_DAYS = 20
REENTRY_COOLDOWN_DAYS = _env_int("REENTRY_COOLDOWN_DAYS", 5)
LIMIT_ORDER_TIMEOUT_MINUTES = _env_int("LIMIT_ORDER_TIMEOUT_MINUTES", 15)
EXIT_LIMIT_TIMEOUT_MINUTES = _env_int("EXIT_LIMIT_TIMEOUT_MINUTES", 2)

OPTION_TYPE = "put"  # buy puts to open; sell owned puts to close
CONTRACT_QTY = 1

SCAN_INTERVAL_SECONDS = 300

LOG_FILE = "logs/options_bot.log"
ANALYTICS_FILE = "logs/trade_analytics.csv"
