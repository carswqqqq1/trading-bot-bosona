# Paper test E

Public Polymarket data only. Wallet `0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed`
(@bosona). No live orders. Cash was not reset.

Rules kept for both samples: buy his exact side and market only when his price
is inside 0.40–0.60 inclusive and 5 shares are offered at his price or better;
skip and record the cent gap when the ask is already worse; sell inside the
same 60 seconds when the bid is above paper cost and the sale nets a gain.

Cent gap is `(our price - his price) * 100`. A negative gap means our paper
price was better than his fill.

Phoenix is America/Phoenix, seven hours behind UTC. These fills are 2026-10-01
UTC and 2026-09-30 in Phoenix.

## Sample 1, activity poll

Started 2026-10-01 01:16:33 UTC / 2026-09-30 18:16:33 Phoenix, cash $39.
Log: [paper-e-2026-10-01.jsonl](paper-e-2026-10-01.jsonl).

| Window end (UTC / Phoenix) | What happened | Cash | Realized | Unrealized | Equity |
| --- | --- | ---: | ---: | ---: | ---: |
| 01:17:33 / 18:17:33 | Two 5-share buys, then a gain sell. Later 0.66 buys skipped. | 39.16351 | +0.16351 | 0 | 39.16351 |
| 01:18:33 / 18:18:33 | Two 5-share buys, then a gain sell. One extra in-band fill skipped on the $5 outcome cap. | 39.24151 | +0.24151 | 0 | 39.24151 |
| 01:19:33 / 18:19:33 | No new trade from him. | 39.24151 | +0.24151 | 0 | 39.24151 |
| 01:20:33 / 18:20:33 | No new trade from him. | 39.24151 | +0.24151 | 0 | 39.24151 |
| 01:21:33 / 18:21:33 | No new trade from him. | 39.24151 | +0.24151 | 0 | 39.24151 |

Decision latency on this poll was 1.469–2.679 seconds. First sight of a fill was 1.260–2.469 seconds.

### Actions

| His fill UTC / Phoenix | Paper action UTC / Phoenix | Decision | His price | Our price | Cent gap | Shares | Latency s | Realized | Unrealized | Cash |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 01:17:21 / 18:17:21 | 01:17:22 / 18:17:22 | taken, BUY Down `btc-updown-5m-1790817300` | 0.48 | 0.38 | -10.00 | 5 | 1.488 | 0 | -0.21405 | 37.01754 |
| 01:17:21 / 18:17:21 | 01:17:22 / 18:17:22 | taken, BUY Down same market | 0.4800000153 | 0.38 | -10.00 | 5 | 1.502 | 0 | -0.42809 | 35.03508 |
| 01:17:21 / 18:17:21 | 01:17:22 / 18:17:22 | skipped, outcome cap | 0.48 | 0.38 to 0.37 | -10 to -11 |  | 1.530–1.602 | 0 | -0.53 | 35.03508 |
| 01:17:23 / 18:17:23 | 01:17:25 / 18:17:25 | skipped, outcome cap | 0.48 | 0.22 | -26.00 |  | 2.679 | 0 | -1.98105 | 35.03508 |
| 01:17:21 / 18:17:21 | 01:17:29 / 18:17:29 | sold, bid above cost | 0.48 | 0.43 | -5.00 | 10 | 8.552 hold | +0.16351 | 0 | 39.16351 |
| 01:17:30 / 18:17:30 | 01:17:32 / 18:17:32 | skipped, outside 0.40–0.60 (four fills) | 0.66 |  |  |  | 2.272–2.285 | +0.16351 | 0 | 39.16351 |
| 01:17:53 / 18:17:53 | 01:17:54 / 18:17:54 | taken, BUY Down `btc-updown-15m-1790817300` | 0.42 | 0.34 | -8.00 | 5 | 1.469 | +0.16351 | -0.20593 | 37.38497 |
| 01:17:53 / 18:17:53 | 01:17:54 / 18:17:54 | taken, BUY Down same market | 0.42 | 0.34 | -8.00 | 5 | 1.478 | +0.16351 | -0.41185 | 35.60643 |
| 01:17:54 / 18:17:54 | 01:17:55 / 18:17:55 | skipped, outcome cap | 0.42 | 0.35 | -7.00 |  | 1.695 | +0.16351 | -0.31416 | 35.60643 |
| 01:17:53 / 18:17:53 | 01:17:57 / 18:17:57 | sold, bid above cost | 0.42 | 0.38 | -4.00 | 10 | 4.802 hold | +0.24151 | 0 | 39.24151 |

The 0.66 skips did not read the book, so they have no cent gap. The ask was not the reason for the skip.

## Sample 2, public trade stream

Same database, so cash continued at $39.24151. Started 2026-10-01 01:26:18 UTC /
2026-09-30 18:26:18 Phoenix.
Log: [paper-e-ws-2026-10-01.jsonl](paper-e-ws-2026-10-01.jsonl).

| Window end (UTC / Phoenix) | What happened | Cash | Realized | Unrealized | Equity |
| --- | --- | ---: | ---: | ---: | ---: |
| 01:27:19 / 18:27:19 | Two 5-share buys still open. Mark was under cost. | 34.87099 | +0.24151 | -0.73544 | 38.50607 |
| 01:28:18 / 18:28:18 | Same-minute sell cleared that position for a gain. No further trade. | 39.49627 | +0.49627 | 0 | 39.49627 |
| 01:29:18 / 18:29:18 | No new trade from him. | 39.49627 | +0.49627 | 0 | 39.49627 |
| 01:30:18 / 18:30:18 | No new trade from him. | 39.49627 | +0.49627 | 0 | 39.49627 |
| 01:31:18 / 18:31:18 | No new trade from him. | 39.49627 | +0.49627 | 0 | 39.49627 |

### Actions

| His fill UTC / Phoenix | Paper action UTC / Phoenix | Decision | His price | Our price | Cent gap | Shares | Latency s | Realized | Unrealized | Cash |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 01:26:48 / 18:26:48 | 01:26:48 / 18:26:48 | taken, BUY Up `btc-updown-5m-1790817900` | 0.43 | 0.42 | -1.00 | 5 | 0.535 | +0.24151 | -0.21993 | 37.05625 |
| 01:26:48 / 18:26:48 | 01:26:48 / 18:26:48 | taken, BUY Up same market | 0.43 | 0.42 | -1.00 | 5 | 0.933 | +0.24151 | -0.43985 | 34.87099 |
| 01:26:48 / 18:26:48 | 01:26:49 / 18:26:49 | skipped, outcome cap (four fills) | 0.43 | 0.41 | -2.00 |  | 1.058–1.080 | +0.24151 | -0.63705 | 34.87099 |
| 01:26:57 / 18:26:57 | 01:26:57 / 18:26:57 | skipped, outside 0.40–0.60 | 0.65 |  |  |  | 0.990 | +0.24151 |  | 34.87099 |
| 01:27:10 / 18:27:10 | 01:27:10 / 18:27:10 | skipped, outside 0.40–0.60 | 0.65 |  |  |  | 0.886 | +0.24151 |  | 34.87099 |
| 01:26:48 / 18:26:48 | 01:27:22 / 18:27:22 | sold, bid above cost | 0.43 | 0.48 | +5.00 | 10 | 34.003 hold | +0.49627 | 0 | 39.49627 |

First sight of a fill on this stream was 0.308–0.874 seconds. The fastest paper buy was 0.535 seconds after his fill timestamp.

## Totals

| | USD |
| --- | ---: |
| Starting cash | 39.00 |
| Cash | 39.49627 |
| Realized P/L | +0.49627 |
| Unrealized P/L | 0 |
| Equity | 39.49627 |
| $78 reached | No |

No reset. No rule change. The book copied his fills and finished ahead. One window was marked down while a position was still open; the same-minute sell closed it for a gain, and the following windows stayed positive.

## What changed to cut latency

The poll sample decided 1.469 seconds after the fastest in-band fill. Two paper-only changes came before the second sample:

- Read his fills from the public activity trade stream (`wss://ws-live-data.polymarket.com`, topic `activity` / `trades`) and decide when that frame arrives, instead of waiting for the next activity-page poll.
- Reuse the TLS connection for the public book and market reads. Out-of-band buys still skip before a book request.

That cut the fastest in-band decision from 1.469 seconds to 0.535 seconds. The remaining time is his fill timestamp to the public frame, plus the book read that checks the 5-share price. Sell latency in the tables is how long the position was held inside the minute, not how long detection took.
