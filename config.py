import os
import math
from pathlib import Path
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

ORIGINAL_UNDERLYINGS = [
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


# Candidate expansion, not a price-based contract selector. Every candidate must
# still pass the original bearish signal, DTE/delta, liquidity, spread and premium
# gates. No leveraged/inverse funds are added to change the strategy's direction.
AFFORDABLE_UNIVERSE_ADDITIONS = [
    "XLF", "XLE", "XLP", "XLU", "XLRE", "XLB", "KRE", "XBI",
    "EEM", "EFA", "FXI", "EWZ", "GDX", "GDXJ", "SLV", "IAU", "TLT", "HYG",
    "F", "GM", "T", "VZ", "KMI", "SOFI", "SNAP", "UBER", "RIVN",
    "PINS", "CCL", "AAL", "DAL", "WFC",
]
# Funds do not have corporate earnings dates; stock earnings guards remain active.
NON_CORPORATE_UNDERLYINGS = {
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLP", "XLU", "XLRE",
    "XLB", "KRE", "XBI", "EEM", "EFA", "FXI", "EWZ", "GDX", "GDXJ",
    "SLV", "IAU", "TLT", "HYG",
}
EXPANDED_UNDERLYINGS = list(dict.fromkeys(
    ORIGINAL_UNDERLYINGS + AFFORDABLE_UNIVERSE_ADDITIONS
))
UNIVERSE_PROFILE = os.getenv("UNIVERSE_PROFILE", "expanded").strip().lower()
if UNIVERSE_PROFILE not in {"original", "expanded"}:
    raise ValueError("UNIVERSE_PROFILE must be 'original' or 'expanded'")
UNDERLYINGS = list(ORIGINAL_UNDERLYINGS if UNIVERSE_PROFILE == "original"
                   else EXPANDED_UNDERLYINGS)

DOLLARS_PER_TRADE = 100


def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    return int(os.getenv(name, str(default)))


def _env_float(name, default):
    return float(os.getenv(name, str(default)))


# One research allocation shared by both long-put signal variants. Broker account
# equity/buying power never increases these limits.
STRATEGY_ID = "long_put"
VIRTUAL_STARTING_CAPITAL = _env_float("VIRTUAL_STARTING_CAPITAL", 25000.0)
MAX_OPTION_PREMIUM_PER_TRADE = _env_float("MAX_OPTION_PREMIUM_PER_TRADE", 500.0)
MAX_CONTRACTS_PER_TRADE = _env_int("MAX_CONTRACTS_PER_TRADE", 1)
if not math.isfinite(VIRTUAL_STARTING_CAPITAL) or VIRTUAL_STARTING_CAPITAL <= 0:
    raise ValueError("VIRTUAL_STARTING_CAPITAL must be positive and finite")
if not 0 < MAX_OPTION_PREMIUM_PER_TRADE <= 500:
    raise ValueError("Paper/live premium cap must be in (0, 500]; use backtest CLI for larger experiments")
if MAX_CONTRACTS_PER_TRADE != 1:
    raise ValueError("LongPutBot research requires exactly one contract per trade")

MAX_POSITIONS = _env_int("MAX_POSITIONS", 2)
ENABLE_NEW_ENTRIES = _env_bool("ENABLE_NEW_ENTRIES", False)
# Legacy settings may tighten, but never bypass, the canonical premium ceiling.
MAX_PREMIUM_PER_TRADE = min(
    _env_float("MAX_PREMIUM_PER_TRADE", MAX_OPTION_PREMIUM_PER_TRADE),
    MAX_OPTION_PREMIUM_PER_TRADE,
)
MAX_TOTAL_OPTION_PREMIUM = _env_float("MAX_TOTAL_OPTION_PREMIUM", 1000.0)
REGULAR_MAX_PREMIUM_PER_TRADE = min(
    _env_float("REGULAR_MAX_PREMIUM_PER_TRADE", MAX_PREMIUM_PER_TRADE),
    MAX_PREMIUM_PER_TRADE,
)
MAX_100_PREMIUM_PER_TRADE = min(
    _env_float("MAX_100_PREMIUM_PER_TRADE", MAX_PREMIUM_PER_TRADE),
    MAX_PREMIUM_PER_TRADE,
)
for _name in ("MAX_PREMIUM_PER_TRADE", "MAX_TOTAL_OPTION_PREMIUM",
              "REGULAR_MAX_PREMIUM_PER_TRADE", "MAX_100_PREMIUM_PER_TRADE"):
    if not math.isfinite(globals()[_name]) or globals()[_name] <= 0:
        raise ValueError(f"{_name} must be positive and finite")
UNDERLYING_TRAILING_STOP_PERCENT = _env_float(
    "UNDERLYING_TRAILING_STOP_PERCENT", 0.03
)
PAPER_STRATEGIES = (
    {
        "name": "regular",
        "signal": "daily_trend",
        "max_premium": REGULAR_MAX_PREMIUM_PER_TRADE,
        "underlying_trailing_stop": UNDERLYING_TRAILING_STOP_PERCENT,
        "underlying_take_profit": 0.08,
        "max_holding_days": 20,
    },
    {
        "name": "oasis",
        "signal": "intraday_oasis",
        "max_premium": MAX_100_PREMIUM_PER_TRADE,
        "underlying_trailing_stop": UNDERLYING_TRAILING_STOP_PERCENT,
        "underlying_take_profit": None,
        "max_holding_days": None,
        "option_stop_loss": 0.20,
        "intraday": True,
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
    "financials": {"JPM", "BAC", "GS", "MS", "C", "WFC", "SOFI", "XLF", "KRE"},
    "energy": {"XOM", "CVX", "COP", "SLB", "KMI", "XLE"},
    "healthcare": {"UNH", "LLY", "JNJ", "PFE", "MRK", "XBI"},
    "consumer_industrial": {
        "COST", "WMT", "HD", "DIS", "BA", "F", "GM", "UBER", "RIVN",
        "CCL", "AAL", "DAL", "XLP",
    },
    "communications": {"T", "VZ", "SNAP", "PINS"},
    "international": {"EEM", "EFA", "FXI", "EWZ"},
    "precious_metals": {"GDX", "GDXJ", "SLV", "IAU"},
    "rates_credit": {"TLT", "HYG"},
    "utilities": {"XLU"},
    "real_estate": {"XLRE"},
    "materials": {"XLB"},
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
BACKTEST_STARTING_CASH = VIRTUAL_STARTING_CAPITAL  # compatibility alias; CLI can override
OPTION_STOP_LOSS_PERCENT = _env_float("OPTION_STOP_LOSS_PERCENT", 0.30)
# Oasis-only premium trail; regular keeps its existing underlying trail.
OPTION_TRAILING_STOP_PERCENT = _env_float("OPTION_TRAILING_STOP_PERCENT", 0.20)
if not 0 <= OPTION_TRAILING_STOP_PERCENT < 1:
    raise ValueError("OPTION_TRAILING_STOP_PERCENT must be at least zero and less than one")
OPTION_TAKE_PROFIT_PERCENT = 1.00
EXIT_DTE = _env_int("EXIT_DTE", 30)
MAX_HOLDING_DAYS = 20
REENTRY_COOLDOWN_DAYS = _env_int("REENTRY_COOLDOWN_DAYS", 5)
LIMIT_ORDER_TIMEOUT_MINUTES = _env_int("LIMIT_ORDER_TIMEOUT_MINUTES", 15)
EXIT_LIMIT_TIMEOUT_MINUTES = _env_int("EXIT_LIMIT_TIMEOUT_MINUTES", 2)

OPTION_TYPE = "put"  # buy puts to open; sell owned puts to close
CONTRACT_QTY = MAX_CONTRACTS_PER_TRADE

SCAN_INTERVAL_SECONDS = 60
LEGACY_SWING_STRATEGY = {
    "name": "max_100", "signal": "daily_swing",
    "underlying_trailing_stop": UNDERLYING_TRAILING_STOP_PERCENT,
    "underlying_take_profit": 0.06, "max_holding_days": 15,
}

# Keep real-money research records separate if live mode is explicitly selected.
PROJECT_DIR = Path(__file__).resolve().parent
_LOG_DIR = PROJECT_DIR / ("logs" if ALPACA_PAPER else "logs/live")
LOG_FILE = f"{_LOG_DIR}/options_bot.log"
ANALYTICS_FILE = f"{_LOG_DIR}/trade_analytics.csv"
REJECTED_TRADES_FILE = f"{_LOG_DIR}/rejected_trades.csv"
RESEARCH_REPORT_FILE = f"{_LOG_DIR}/long_put_research.json"
