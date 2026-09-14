## Trailing stops

Option-premium trailing stops are enabled for **oasis only** with
`OPTION_TRAILING_STOP_PERCENT=0.20`:

- Bought calls/puts (direct and inverted): sell when the observed option premium
  falls 20% below its highest observed premium for the current holding.
- Sold covered calls/cash-secured puts: buy back when the ask rebounds 20% above
  its lowest observed buyback price for the current holding.

Regular retains its previous controls: direct/inverted use their existing 30%
fixed option stops and 3% underlying trails; covered/secured keep their 2x-credit
fixed stops without an option-premium trail. Oasis alone uses the 20% fixed stop
and 20% premium trail. The trail starts from its entry premium.
Long-option highs are rebuilt from confirmed fills and durable premium snapshots;
short-option lows are persisted in a small ledger table keyed to the entry order.
The trail never loosens as prices reverse, survives restarts, and resets for a new
trade. Existing fixed stops, regular underlying-price trails, technical exits,
Oasis closing times, collateral controls, and the shared loss block remain active.
Stops are monitored limit-order exits and do not guarantee execution at the trigger.
An existing short option starts from its entry credit/current ask because earlier
unrecorded intraday lows cannot be reconstructed.

# Regular and Oasis runtime update

Active variants: **regular** and **oasis**. Oasis buys puts using **bearish**
EMA/momentum signals and exits on a bullish cloud break or fading bearish momentum.
Its option-premium stop is **20% below the filled purchase price**; regular keeps
its 30% default stop. The former `max_100` daily swing variant accepts no new trades,
but existing positions retain its original daily exits and historical label.

The regular variant keeps the existing daily entry and exit rules. Oasis uses
completed regular-session 5-minute candles: a 9/21 EMA cloud, both EMAs moving in
the trade direction, RSI(14), and a strengthening MACD(12/26/9) histogram. Bullish
entries require RSI between 50 and 70; bearish entries require RSI between 30 and
50. Only a fresh false-to-true setup can enter. Stale, incomplete and prior-session
bars cannot trigger entries. The existing daily market filter and contract-quality,
earnings, ownership and capital limits still apply.

Oasis stops opening entries 30 minutes before Alpaca's reported stock-session close
and starts closing its options 15 minutes before close, including shortened
sessions. Pending entries are canceled at the cutoff; cancellation remains pending
until confirmed by the broker. Any overnight remainder is closed on the next open
cycle. Momentum/cloud breakdown can exit earlier. Risk/order checks run every
60 seconds plus processing time. The original expiration windows are retained.
These are monitored limit-order exits, not guaranteed fills or maximum losses.
Delayed indicative option quotes limit intraday paper-execution realism.

A confirmed loss blocks all new contracts on that underlying through 30 calendar
days after the loss; reentry is permitted on day 31. The check applies across
regular, oasis and historical variants, survives restarts, and is enforced at the
final entry gate. Winning exits do not start/reset the loss block. Regular keeps
its existing ordinary reentry cooldown; oasis uses only the loss block and signal
bar deduplication. Pending closing orders must reconcile before reentry.

By default the block is also shared across all four sibling bots via read-only
checks of these ledgers:

- `options_direct/logs/trade_analytics.csv`
- `options_inverted/logs/trade_analytics.csv`
- `options_covered/logs/trades.sqlite3`
- `options_secured/logs/options_secured.sqlite3`

Set `LOSS_GUARD_SCOPE=bot` for checks only within each bot. For custom ledger
locations, set `LOSS_LEDGER_PATHS` to a JSON object mapping bot directory names to
absolute paths. Missing default ledgers are ignored; existing unreadable ledgers
block entries until readable. This conservatively combines the configured ledgers
without inferring whether they belong to the same taxpayer/account. Use ledgers
from the intended trading environment. No external/manual accounts are inspected.
This is not wash-sale tax accounting: it does not resolve substantially-identical
instruments, the pre-loss purchase window, or unrecorded transactions.

Historical daily backtests remain historical research, not Oasis simulations, and
do not simulate the new shared loss block. Existing trade records are preserved.
The changes load on the next restart; installation does not start or restart bots.

---

Existing setup and historical research reference:

# OptionsInverted / LongPutBot

Long-put adaptation of [OptionsDirect](https://github.com/aspittman/options_direct),
based on commit `66737da21b00b51b25d93cca30388030d76f3a51`.
The bot buys puts on bearish setups and sells only owned long puts to close.
The strategy identifier is `long_put`; `regular` and `max_100` remain signal
variant names, not separate funded portfolios.

## Capital and risk

```dotenv
ALPACA_PAPER=true
VIRTUAL_STARTING_CAPITAL=25000
MAX_OPTION_PREMIUM_PER_TRADE=500
MAX_CONTRACTS_PER_TRADE=1
MAX_TOTAL_OPTION_PREMIUM=1000
MAX_POSITIONS=2
MAX_POSITIONS_PER_CORRELATION_GROUP=1
```

Both variants share one virtual $25,000 allocation. Broker equity, cash and buying
power are displayed only as account context and never increase this allocation or
the permitted trade size. Capital employed is entry option premium × quantity ×
100. Available virtual cash is starting allocation plus confirmed realized P/L,
less open premium and pending-buy reservations. Exposure cannot exceed either
the starting allocation or the existing total-premium risk limit.

**The existing $1,000 combined premium and two-position limits remain active.**
The $25,000 allocation does not authorize $25,000 of simultaneous option exposure.
Profits do not raise the $500 entry ceiling or the fixed exposure ceilings.
Pending cancellations retain their reservation until the broker confirms terminal
status; partial fills reserve only the unfilled remainder in addition to owned cost.

A $3.40 quote costs $340 for one contract and fits. A $6.25 quote costs $625 and is
rejected. The final check uses the actual cent-rounded midpoint limit: a $5.005
midpoint becomes $5.01, or $501, and is rejected. Contract selection still uses DTE,
delta/moneyness, liquidity and spread quality before capital is considered. An
expensive preferred contract is never replaced with a cheap inferior strike.

`MAX_PREMIUM_PER_TRADE`, `REGULAR_MAX_PREMIUM_PER_TRADE`, and
`MAX_100_PREMIUM_PER_TRADE` remain compatibility settings. They can tighten the
canonical premium limit, never raise or disable it. Nonpositive limits fail
configuration validation. Paper/live configuration rejects premium ceilings over
$500 and contract counts other than one; use the historical CLI for larger premium
experiments. `BACKTEST_STARTING_CASH` is now a code compatibility alias for
`VIRTUAL_STARTING_CAPITAL`; its old environment setting no longer overrides the
allocation.

## Expanded research universe

The default `UNIVERSE_PROFILE=expanded` keeps all 40 original symbols and adds
32 candidates, for 72 total. Both regular and swing use this universe, as do the
historical runs. Set `UNIVERSE_PROFILE=original` to compare with the old watchlist.
The original ordering is preserved; contracts are not prioritized by cheapness.

| Category | Added symbols |
|---|---|
| Sector/industry ETFs | XLF, XLE, XLP, XLU, XLRE, XLB, KRE, XBI |
| International ETFs | EEM, EFA, FXI, EWZ |
| Precious-metals funds | GDX, GDXJ, SLV, IAU |
| Bond ETFs | TLT, HYG |
| Stocks | F, GM, T, VZ, KMI, SOFI, SNAP, UBER, RIVN, PINS, CCL, AAL, DAL, WFC |

These broaden the lower-notional candidate pool; share prices and actual put
premiums change, and membership does not certify affordability or liquidity.
Issuer fund classifications are available from [State Street](https://www.ssga.com/us/en/intermediary/capabilities/equities/sector-investing/select-sector-etfs)
and [iShares](https://www.ishares.com/us/products/etf-investments).
No leveraged or inverse ETFs were added. Existing expensive symbols remain eligible
when their preferred contracts qualify.

Added symbols have explicit correlation groups; related additions share existing
sector limits. All added ETFs/trusts bypass corporate earnings lookups, while
stocks retain the earnings guard. The SPY bearish regime, fresh daily regular/swing
signals, 60–90 DTE, −0.60 delta target, liquidity/spread tests, $500 premium ceiling,
one-contract size, and portfolio limits are unchanged. An expanded universe will
still produce no entries when the SPY regime blocks trading. Restart the running
bot to load the expanded list; startup logs show the profile and symbol count.

## Strategy rules

Signals use completed daily bars. The SPY regime requires price and its 50-day
average below its 200-day average. The two unchanged entry variants are:

- `regular`: price below the falling 50-day average and below the 200-day average,
  with bearish MACD confirmation; 8% underlying-decline target and 20-trading-day hold.
- `max_100`: bearish 50/200-day regime, rejection back below the 20-day EMA after
  trading above it, 10-day EMA confirmation, RSI 35–55 and negative MACD histogram;
  6% underlying-decline target and 15-trading-day hold. Its historical name does not
  mean the premium cap is $100.

Both require a fresh false-to-true signal. Five-weekday reentry cooldowns,
earnings guards, correlation limits, and duplicate restrictions remain in place.
Contracts use 60–90 DTE, target delta −0.60 ±0.10, open interest above 500,
volume above 100, and bid/ask spread below 5% of midpoint. Liquidity and spread
requirements have not been loosened.

A 3% underlying rebound from the lowest observed price triggers the trailing
stop. Other exits include bullish technical reversals, a 30% option-premium stop,
maximum hold, and closing at/before the configured 30-DTE threshold when execution
is available. The option-premium trailing stop is enabled at 20% for Oasis only.
Entries use midpoint day limits; exits use marketable bid limits. Unfilled entry
orders are canceled after 15 minutes; exit limits after two minutes and retried
after cancellation confirmation. Stops are monitored, not guaranteed fills.

## Shared paper account and ownership

The local fill ledger identifies owned long-put quantities. Account-level stock,
call and short-put positions are ignored. Historical ownership of a symbol does
not allow adoption of a later account position. Automatic legacy adoption has
been disabled. Position displays and exits use the bot's owned quantity and cost
basis, not another strategy's quantity in a net broker position.

Orders use `long_put_<underlying>_<unique-id>` client IDs and explicit
`buy_to_open` / `sell_to_close` intents. Logs include `long_put`; the CSV adds
`bot_strategy` while retaining the variant in `strategy`. Existing local
`regular`/`max_100` put rows and tracked `oi-` orders remain readable. Explicit
foreign strategy identifiers are excluded. Before reconciling or canceling an order,
the broker order must also be a single-leg put with explicit `buy_to_open` for a
buy or `sell_to_close` for a sell. Calls, stock orders, short-put opening/closing
orders, multi-leg orders and missing/unknown intents are ignored even if their
client IDs match. This includes legacy orders without explicit intent; their
reservations remain pending rather than assuming their strategy type.

New orders are blocked if the same contract already exists in the account or has
an open account order. Exits require owned long-put quantity and sufficient broker
long quantity, and will not cancel another bot's orders. The account nets identical
contracts: identifiers cannot prevent another independent bot from concurrently
submitting an opposing order after this bot's check. All bots must honor ownership
and same-contract conflict checks for reliable separation.

Missing or reduced broker inventory is flagged as unresolved. Its entry capital
stays reserved and new entries pause until an owned exit is confirmed or quantity
is reconciled. Missing positions are never automatically classified as worthless
expirations. Do not copy another bot's ledger into this folder.

A local process lock prevents two copies of this bot from spending the same local
allocation. Paper outputs use this project's `logs/`; explicitly configured live
mode uses `logs/live/`. Paths are anchored to this project, including when launched
from another working directory. No live or paper process is started by setup.

## Setup and run

```bash
cd /path/to/options_inverted
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Only if you do not already have a configured .env:
cp .env.example .env
```

Set paper credentials with `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in `.env`.
`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` and `API_KEY`/`SECRET_KEY` are also accepted.
Keep `ALPACA_PAPER=true`. Set `ENABLE_NEW_ENTRIES=true` to enable paper entries;
the default example disables entries while allowing exits to be managed.

```bash
python3 main.py
# Optional restart supervisor:
python3 launcher.py
```

Only run one of these commands. Restart an already running bot to load code or
configuration changes. The bot waits for market hours and can legitimately place
no trades when bearish signals or contract-quality filters do not qualify.

## Rejected opportunities

`logs/rejected_trades.csv` records fresh market/signal-qualified opportunities
blocked by a later guard. Non-bearish scans and continuing signals are not counted
as qualified opportunities. The existing ledger also records `SIGNAL_QUALIFIED`.

Fields include timestamp, strategy, variant, underlying, contract, direction,
strike, expiration, DTE, underlying price, bid/ask/mid, dollar and percentage spread,
option premium, required capital, virtual cash available, rejection reason,
signal date, signal score and market regime. Unavailable fields are blank; the
boolean signal system has no numeric score. Percentages in this research CSV
are percentage points, e.g. `4` means 4%; configuration fractions use `0.04`.

Reasons include `PREMIUM_OVER_LIMIT`, `INSUFFICIENT_LIQUIDITY`, `SPREAD_TOO_WIDE`,
`MAX_CONTRACTS_REACHED`, `MAX_STRATEGY_EXPOSURE_REACHED`, `DUPLICATE_POSITION`,
`NO_VALID_CONTRACT`, and `OTHER` with explanatory details. Multiple failed quality
filters may produce `NO_VALID_CONTRACT`. Capital checks report the first blocker;
`PREMIUM_OVER_LIMIT` means other evaluated guards passed. Broker rejections are
recorded separately when reconciled. Rejected opportunities never become fills or
completed trades. A restart can re-evaluate an unfilled signal, so repeated research
observations may be grouped by variant, underlying and signal date when analyzing.

## Performance

```bash
python3 backtester.py --paper-results
```

This reads the local ledger without contacting Alpaca or placing orders and writes
`logs/long_put_research.json`. The running bot refreshes the same report each cycle.
The report includes:

- Starting/ending virtual capital, realized/unrealized P/L, total return, exposure,
  pending reservations and available virtual cash.
- Return on capital employed, average entry capital, peak concurrent entry capital,
  completed round-trip count, entry count, win rate, average/largest winner and loser,
  expectancy, profit factor, maximum drawdown and average holding days.
- Premium paid, premium lost on net losing completed trades, option return,
  average entry premium, average entry DTE and available spread observations.
- Explicitly confirmed worthless expirations, unresolved positions and rejection counts.

Allocation return = total P/L ÷ starting virtual capital. Return on capital employed
and option return = total P/L ÷ cumulative entry premium. These denominators differ
intentionally. Repeatedly deployed capital counts each entry for ROC; peak capital
employed measures concurrent cost. Partial exits count as one trade when a round
trip finishes. Premium paid includes open trades; trade outcome statistics use
completed trades. No-loss profit factor is null with a status rather than a fabricated
finite value. Missing averages are null.

Unrealized P/L uses the latest recorded marks, with entry-cost fallback for unmarked
positions, and is provisional if marks are stale or positions unresolved. Drawdown
uses sampled ledger marks and fills, not continuous tick data. Worthless expiration
counts require an explicit `EXPIRATION_CONFIRMED` event backed by broker evidence;
this bot does not infer that event from absence and normally seeks to exit by 30 DTE.
Premium-selling metrics such as collateral yield and assignment rate are not
applicable to this long-put strategy.

## Historical capital comparisons

```bash
python3 backtester.py --years 1
python3 backtester.py --years 3 --premium-limits 250 500 750 1000
python3 backtester.py --years 5 --premium-limits 500 --virtual-capital 25000
python3 backtester.py --period 2y --alpaca-options swing --max-candidates 100 --premium-limits 250 500 750 1000
```

Standard runs generate fresh bearish opportunities for both variants, then apply
one shared cash/exposure ledger, duplicate/group limits and reentry cooldowns.
Candidates are identical across premium configurations; rejected trades cannot
suppress later fresh signals. Capital employed is entry premium × 100 in both live
paper and historical accounting. The explicit premium comparison overrides legacy
per-variant premium settings only; the existing combined exposure and position
limits still apply. Daily strategies require `--interval 1d`.

Each configuration writes `logs/backtest_long_put_<cap>_summary.json`, `_trades.csv`,
`_equity.csv`, and `_rejected.csv`. Summaries show qualified signals, executed trades,
rejections by reason, and the same portfolio statistics/denominators used for paper.
Historical equity starts at the virtual allocation. The Alpaca repricing mode uses
`logs/alpaca_long_put_<variant>_<cap>_*` and limits counts to the queried candidate
subset. Historical output is separate from paper fill and rejection ledgers.

These are research estimates, not validated expected returns:

- Synthetic option prices approximate puts and do not contain historical chains,
  spreads, volume or open interest. Historical earnings guards are unavailable.
  Liquidity/spread rejection counts and average spreads are therefore unknown,
  not claims that all candidates passed those live filters.
- Entry fills are modeled at the signal-bar close; real execution occurs after
  that completed bar. Limit fill probability, slippage and fees are not simulated.
- Alpaca repricing selects the preferred strike/DTE before checking price and
  requires exact entry/exit-date bars. It does not shop for cheap substitutes or
  use a previous day's bar as an exit. Historical Greeks/quote-quality data are
  unavailable; strike selection approximates target delta.
- Alpaca repricing validates candidate entry/exit dates from the synthetic/technical
  path; it does not replay actual-option intratrade stops. Historical drawdown is
  closed-trade equity only. These limitations must be considered before comparing
  either backtest with actual paper fills.
- `--compare-signals` is retained as an isolated $100 bearish-underlying diagnostic;
  it is not an option portfolio or a capital-limit experiment.

No profitability or parameter optimization is claimed by this implementation.

## Offline checks

```bash
APCA_API_KEY_ID=test APCA_API_SECRET_KEY=test python3 -m unittest -q
```

Tests use dummy credentials, temporary research ledgers and mocked broker calls.
They place no orders. Configuration and ownership assertions cover the $500 cap,
rounded limits, one-contract size, pending/partial fills, cancellation confirmation,
shared allocation, foreign positions/orders, rejection data and historical comparisons.
