# OptionsInverted

Long-put adaptation of [OptionsDirect](https://github.com/aspittman/options_direct),
based on commit `66737da21b00b51b25d93cca30388030d76f3a51`.

Buys puts in bearish markets and sells owned contracts to close. The SPY regime
requires price and its 50-day average below its 200-day average. Contracts target
**-0.60 delta ±0.10**, with the reference liquidity filters and 60–90 DTE window.
Underlying profit targets are declines of 8% (regular) or 6% (swing). A 3% rebound
from the lowest observed underlying price triggers the trailing stop. Technical
exits trigger on bullish reversals. Option premium P/L retains the normal long
position direction: rising put premiums produce gains.

The bearish rules are an initial adaptation, not empirically optimized parameters
or demonstrated profitable settings. No historical performance is claimed.
The inherited backtests are approximations: they enter on signal-bar closes and
do not fully reproduce the live regime filter, fresh-signal/cooldown gating, or
all execution and exit rules. Historical repricing does not enforce historical
delta or liquidity filters and can use nearby available bars. Validate those
limitations before interpreting results as expected live returns.

This project uses its own local analytics ledger and `oi-` order tags. Do not copy
the call bot's logs into this project. Paper trading is the default and new entries
are disabled until explicitly enabled in your configuration.

## Setup

```bash
cd /path/to/options_inverted

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Alpaca paper trading credentials:

```bash
APCA_API_KEY_ID=your_alpaca_api_key
APCA_API_SECRET_KEY=your_alpaca_secret_key
ALPACA_PAPER=true
```

The bot also accepts `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` or the older
`API_KEY`/`SECRET_KEY` names, but Alpaca's `APCA_*` names are preferred.

Run the bot:

```bash
python main.py
```

Optional option-risk settings (shown with defaults):

```bash
ENABLE_NEW_ENTRIES=false
EXIT_DTE=30
OPTION_STOP_LOSS_PERCENT=0.30
OPTION_TRAILING_STOP_PERCENT=0
UNDERLYING_TRAILING_STOP_PERCENT=0.03
REENTRY_COOLDOWN_DAYS=5
LIMIT_ORDER_TIMEOUT_MINUTES=15
EXIT_LIMIT_TIMEOUT_MINUTES=2
MAX_PREMIUM_PER_TRADE=500
REGULAR_MAX_PREMIUM_PER_TRADE=500
MAX_100_PREMIUM_PER_TRADE=500
MAX_TOTAL_OPTION_PREMIUM=1000
MAX_POSITIONS=2
MAX_POSITIONS_PER_CORRELATION_GROUP=1
BACKTEST_STARTING_CASH=2500
ALLOW_DUPLICATE_CONTRACTS=false
ALLOW_MULTIPLE_CONTRACTS_PER_UNDERLYING=false
```

When `ENABLE_NEW_ENTRIES=false`, the bot continues reconciling fills and managing
all existing exits, but it submits no new buy orders. Set it to `true` only when
you intentionally resume paper entries. `MAX_POSITIONS=2` is enforced globally
across both named strategies, and the total-premium limit is also shared.

Percent settings are decimal fractions. Position limits and premium totals apply
only to option contracts submitted by OptionsInverted; stock positions and other
bots' positions are excluded. The analytics CSV records realized and unrealized
P/L in separate columns and the cycle log reports results both by contract and
by underlying.

The live paper bot runs two named daily variants in the same Alpaca paper account.
Both use completed daily candles and 60–90 DTE puts, so the live and historical
indicator periods now represent the same timeframe:

- `regular` is the daily trend control: price below falling 50/200-day averages
  with negative MACD confirmation. It holds for at most 20 trading days.
- `max_100` is the daily rally-rejection swing candidate: bearish 50/200-day regime,
  20-day EMA rejection, 10-day EMA confirmation, RSI 35–55, and negative MACD
  histogram. It uses a 3% underlying stop, 6% target, and 15-day maximum hold.

Entries require a fresh false-to-true signal on a newly completed daily candle,
and an underlying cannot be re-entered by the same strategy for five trading days
after an exit. Entries use midpoint day-limit prices and are canceled if they
remain unfilled for 15 minutes. Risk exits use marketable limits at the current
bid and are repriced after two minutes if necessary.

Live daily candles come from Alpaca's IEX stock feed. Entry signals are evaluated
once for each newly completed daily candle; the five-minute runtime loop continues
to reconcile orders and monitor exits. Each loop ends with a concise cycle summary.

Both variants cap entry premium at $500. Across the two variants, at most two
positions and $1,000 of entry premium may be open. All contracts are closed by 30
DTE, and the 30% option stop is catastrophe protection in addition to the
underlying and technical exits. The option trailing stop is disabled by default;
underlying and completed-daily technical signals drive normal exits. The stock
stop trails 3% above the lowest underlying price observed after entry, never
moves upward, and is rebuilt from the analytics ledger after a restart.
Only one open or pending position is allowed from each configured correlation
group (broad indexes, technology, financials, energy, healthcare, and
consumer/industrial), preventing both slots from expressing essentially the same
sector bet.

Both variants submit separately tagged paper orders. Alpaca combines quantities
when both variants own the same contract, while `logs/trade_analytics.csv` keeps
the confirmed fill price and virtual quantity for each variant. Runtime summaries
include `by_strategy` realized and unrealized P/L based on those paper fills.
Changing `MAX_PREMIUM_PER_TRADE` is retained for compatibility with older setups;
the two live variants use the two strategy-specific settings above.

Run the options backtester:

```bash
python backtester.py --years 1
python backtester.py --years 3
python backtester.py --years 5
python backtester.py --years 5 --compare-signals
python backtester.py --years 2 --alpaca-options swing --max-candidates 100
```

`--compare-signals` compares the existing MA/MACD rules with an experimental
daily rally rejection swing setup using $100 of bearish underlying exposure per trade. This
isolates entry/exit quality from synthetic option pricing; it is not an option
return simulation and does not authorize changing the live strategy by itself.

`--alpaca-options` uses actual Alpaca daily option bars and expired contract
metadata instead of theoretical option prices. Alpaca option history begins in
February 2024. Candidates without a real entry/exit bar or a qualifying contract
under the premium ceiling are skipped; the command never fabricates a fill.

Each standard run prints two summaries: the daily trend control and the daily
swing variant, both subject to their configured premium limits. The trend results
are written to `logs/options_backtest_trades.csv` and
`logs/options_backtest_equity_curve.csv`; the daily-swing results are written to
`logs/options_backtest_trades_100_max.csv` and
`logs/options_backtest_equity_curve_100_max.csv`. Each summary includes win rate,
total P/L, profit factor, expectancy, maximum drawdown, and symbol-level results.
Historical backtests remain separate from live paper analytics: they provide many
years of fast, estimated testing, while the live analytics file measures the
actual fills returned by Alpaca paper trading from this point forward.
Both historical variants now enforce starting cash, the configured maximum of two
concurrent positions, the shared total-premium ceiling, and their per-trade premium
limits. Option prices remain estimates rather than historical option-chain quotes.

View both live paper strategies without placing orders or running a historical
simulation:

```bash
python3 backtester.py --paper-results
```

This reports confirmed completed trades, win rate, realized and unrealized P/L,
open virtual positions, and pending orders separately for `regular` and
`max_100`.

## Offline checks

With dependencies installed, run:

```bash
APCA_API_KEY_ID=test APCA_API_SECRET_KEY=test python -m unittest -q
```

The suite uses dummy credentials and mocked broker calls; it places no orders.
