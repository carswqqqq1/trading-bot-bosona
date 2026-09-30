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
The lookback starts one hour before launch and remains fixed during that run.

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
