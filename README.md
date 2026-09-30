# Polymarket → Future.news BTC copy bot: paper prototype

Watches a public Polymarket trading wallet for confirmed 5-minute, 15-minute,
and hourly BTC Up/Down trades. New BUYs produce proposed limit orders if market identity,
outcome token, expiry, price drift, available ask depth, minimum order size, and
the daily proposal budget pass checks. Decisions persist in SQLite across
restarts. Only HTTP GET requests are made. No signing keys are required.

This is the first component of a copy bot, not a live trading integration.
Future.news integration is pending confirmation of the intended execution venue
and its supported authentication/API. SELLs are recorded as observations; they
require follower inventory tracking before automated exits can be implemented.

`paper.py` adds a separate cash-and-inventory simulator for both BUYs and SELLs,
using a $48 paper balance. It uses current Polymarket quotes, not Future orders.

## Run

Python 3.10+; standard library only.

The included `config.bosona.paper.json` selects
[@bosona](https://polymarket.com/@bosona), whose trading wallet was confirmed by
the [official public profile API](https://gamma-api.polymarket.com/public-profile?address=0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed):
`0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed`. It uses illustrative paper settings.

```bash
cp config.example.json config.json
# Edit leader_wallet and your paper-test settings in config.json.
python bot.py --config config.json --once
python bot.py --config config.json
python -m unittest discover -s tests -v
python demo.py  # Synthetic example; works without network or a wallet.
python bot.py --config config.bosona.paper.json --once
python latency.py --duration 1800 --output latency.jsonl
python latency.py --poll-seconds 0.5 --duration 180 --output latency-fast.jsonl
python paper.py --duration 120 --db paper48.sqlite3 --output paper48.jsonl
```

Use the trader's actual Polymarket trading/proxy wallet, not necessarily their
login/signing wallet. The example amounts are test values, not suggested trade
sizes. `max_price_drift=0.02` allows a BUY limit at most two cents per share above
the leader's price; it is an absolute price difference, not two percent.
`max_daily_proposed_usd` limits proposed BUY costs per UTC day, excluding fees.
Changing settings requires a new `--db` path. Run one process per database.

## What the output means

`PROPOSED_BUY` is a candidate, not a fill. The limit price is rounded down to the
market tick; available depth is checked across all asks within that limit.
Fees, quote changes, actual execution, inventory, PnL, settlement, redemptions,
loss limits, and live-order reconciliation are not simulated. The journal can
be inspected with SQLite; proposals never move money.

Polling observes confirmed activity after publication. It does not see a
leader's pending orders, cancellations, or instantaneous fills. Each poll
refetches a complete bounded time window with cursor pagination, permitting
late indexing while signals remain within the configured maximum age.
Signals published later than that window are missed intentionally. The Data
API lacks per-fill log indexes: deduplication uses stable trade fields and
multiplicity, but cannot promise exact identity if the upstream feed revises
or aggregates rows. A production implementation needs that addressed.

The user requested simultaneous BUY/SELL copies with zero latency. That
requirement is technically unattainable: source publication, network transport,
and follower execution each add delay. The agreed first step is measuring
public detection delay in paper mode. Identical follower fills cannot be
guaranteed.

`latency.py` observes both BUYs and SELLs, excludes activity already present at
startup, and records each new trade's first observation in JSONL. Its summary
contains median and p95 source-to-observation delay when samples exist. Source
timestamps have one-second precision; the measurement includes confirmation,
indexing, polling interval, and request time. It is not exchange matching
latency, Future execution latency, or a guarantee about future performance.
No samples means no estimate. Each run creates a fresh baseline and overwrites
the chosen output file. Use a new filename to preserve earlier runs.
The startup baseline covers the preceding hour of activity.

After startup, latency observation fetches the latest 120 seconds of source
activity; publication delayed beyond that window is missed. The faster option
schedules request starts rather than adding sleep after request completion.
Errors back off to at least two seconds. A paired live sample of eight BUYs
reduced median public detection delay from 2.154 to 1.611 seconds; this short
sample establishes neither typical latency nor SELL latency. See `measurements`.

Hourly timing comes from Gamma's timezone-aware `eventStartTime` and `endDate`,
not the market creation date or a guessed offset for the ET market slug.

Books whose timestamp is older than `max_signal_age_seconds` are conservatively
rejected. A timestamp can represent the last update rather than the retrieval
time, so otherwise quiet books may be skipped.

## Verified integration references

- [Polymarket wallet activity](https://docs.polymarket.com/trading/wallet-activity)
- [Data API v2 schema](https://data-api.polymarket.com/v2/openapi.json)
- [Market details](https://docs.polymarket.com/market-data/market-details)
- [Order books](https://docs.polymarket.com/market-data/prices-order-books)
- [Trading authentication and orders](https://docs.polymarket.com/trading/quickstart)

Documentation was inspected on 2026-09-30. The public profile API and Data API
returned live Bosona data; a specific hourly Gamma market also returned valid
data. A single paper poll succeeded without new qualifying signals;
live end-to-end proposal generation has not been verified. Tests use
representative documented responses. Future's public site references
Polymarket markets/account imports; that does not establish a supported
third-party order API. See [Future integration findings](FUTURE-INTEGRATION.md).

## $48 paper portfolio

`config.paper48.json` starts with $48, caps each BUY and outcome's fee-inclusive
open cost at $2.40, and caps total open cost at $9.60. This is the chosen test
sizing, not a guarantee of suitable risk or profitability. Minimum share sizes
can cause skips. The simulator consumes current ask depth for BUYs and bid depth
for SELLs, with a two-cent maximum adverse price difference from the leader.
It makes concurrent market/book requests after detecting the source trade.

Fees use the current market's `feesEnabled` and `feeSchedule`, requiring the
documented exponent-one formula `shares × rate × price × (1-price)`. Unknown fee
schedules cause skips. Fees round per consumed price level in the simulation;
actual fills may have different fee rounding, depth, and available prices.

SELLs close the same fraction of copied holdings as the source sells of its
inventory observed during the session. Source BUY quantities are tracked even
when a follower BUY is skipped. Earlier leader holdings are unknown: unmatched
SELLs are refused, and tiny partial exits can be below the market minimum.
Positions that expire or cannot be quoted remain unresolved; automatic
settlement/redemption is not simulated. Total PnL is omitted if any holding
cannot be valued. A liquidation quote is a snapshot, not a realized fill.

`source_price_slippage_cost_usd` compares the same copied share quantity at the
simulated VWAP versus the leader's source fill: positive means a disadvantage,
negative an improvement. It does not isolate latency from spread, liquidity,
order size, or other effects. `fee_delta_vs_hypothetical_source_price_usd`
compares simulated fees with hypothetical same-quantity taker fees at the source
price; the leader's actual fee role is unknown. Portfolio output separates
cash, fees, realized PnL, available liquidation marks, and unresolved inventory.

Use a fresh database for a new independent $48 test. Reusing a database resumes
cash, inventory, and duplicate protection. JSONL output appends between runs.
