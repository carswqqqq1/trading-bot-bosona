# Paper C

Public wallet `0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed` (@bosona). Paper fills only. No live orders, private keys, or Brez. Figures are from the runs in [the first log](paperc-2026-10-01.jsonl), [the next log](paperc-next-2026-10-01.jsonl), and [the result file](paperc-result-2026-10-01.json).

## One rule changed

The first sample copied nothing. Window 1 had no trade. Window 2 saw eight of his buys and skipped each one under `paper_c_no_new_buys`. Cash stayed $39, realized P/L $0, unrealized P/L $0. No sell was copied. Window 3 was stopped before it finished.

That filter cannot open a position, so a later sell can never close one. Cash was reset to $39 with no open positions, and one rule changed: copy his buy, same side and same market, at the moment the trade appears, only when the book fills at his price or better and the open cost stays inside the $39 cash. The sell rule was not changed.

## Next sample

Three 120-second windows after the reset.

| | USD |
| --- | ---: |
| Starting cash | 39 |
| Ending cash | 30.94321 |
| Realized P/L | 0 |
| Unrealized P/L at sample end | not quoted |
| Last quotable unrealized P/L (end of window 2) | -1.87871 |
| Last quotable equity (end of window 2) | 37.12129 |
| Open cost | 8.05679 |

Four paper buys filled. No sell was copied. Open cost stayed inside the $39 cash. Window 1 had no trade. Window 3 had no trade.

At the sample end both open positions were inside the expiry buffer (`market_not_started_or_near_expiry`). Settlement was not simulated, so unrealized P/L was not quoted then. The last mark that did quote, at the end of window 2, was equity $37.12129. That is below the $39 start. No second rule was changed.

Goal $78 was not reached.

### Latency, his fill to each paper action

| Action | Latency (seconds) | His price | Our price | Cent gap |
| --- | ---: | ---: | ---: | ---: |
| SKIP, ask above his price | 1.393 | 0.03 | 0.04 | 1.00 |
| SKIP, ask above his price | 1.614 | 0.0300000004 | 0.04 | 0.9999999600 |
| PAPER_BUY | 3.446 | 0.43 | 0.33 | -10.00 |
| PAPER_BUY | 3.636 | 0.4300000249 | 0.31 | -12.0000024900 |
| PAPER_BUY | 3.839 | 0.43 | 0.29 | -14.00 |
| SKIP, five shares above the buy budget | 4.04 | 0.4300000114 | 0.29 | -14.0000011400 |
| SKIP, five shares above the buy budget | 4.23 | 0.4300000311 | 0.28 | -15.0000031100 |
| SKIP, five shares above the buy budget | 4.434 | 0.43 | 0.28 | -15.00 |
| SKIP, ask above his price | 2.516 | 0.650000001 | 0.73 | 7.999999900 |
| PAPER_BUY | 2.414 | 0.65 | 0.62 | -3.00 |

Cent gap is our price minus his price, in cents. A paper buy's our price is the simulated fill. A skip's our price is the book quote used to refuse the fill, not a fill.

## Continued sample

The book was not reset and no rule was changed. The run resumed at cash $30.94321 with the two open positions still on. [Log](paperc-continue-2026-10-01.jsonl).

Both markets were already resolved on the public book. Gamma `outcomePrices` were `["0", "1"]` for outcomes `["Up", "Down"]`. Those published payouts were used. A last trade was not used.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Up | 15 | 0 | 0 | 4.87433 | -4.87433 | 30.94321 | still open until the Down close | no | none; this is not one of his fills |
| Down | 5 | 1 | 5 | 3.18246 | 1.81754 | 35.94321 | 0 | no | none; this is not one of his fills |

After both closes, realized P/L was -$3.05679 and equity was $35.94321.

Three more 120-second windows then copied three buys and no sells. Window 2 had no trade. Window 3 had no trade.

| | USD |
| --- | ---: |
| Ending cash | 34.08600 |
| Realized P/L | -3.05679 |
| Unrealized P/L at sample end | not quoted |
| Last quotable unrealized P/L (end of window 2) | -0.31001 |
| Last quotable equity (end of window 2) | 35.63320 |
| Open cost still on the book | 1.85721 |

The remaining position is 15 Up shares of `btc-updown-15m-1790818200`, cost $1.85721. It had not resolved, so it stayed open. The sample-end book quote was stale, so unrealized P/L was not quoted then. Equity did not reach $78.

Latency in seconds from his fill to each paper action in these windows:

| Action | Latency (seconds) | His price | Our price | Cent gap |
| --- | ---: | ---: | ---: | ---: |
| PAPER_BUY | 1.453 | 0.09 | 0.09 | 0.00 |
| SKIP, ask above his price | 1.44 | 0.09 | 0.15 | 6.00 |
| PAPER_BUY | 2.406 | 0.16 | 0.16 | 0.00 |
| PAPER_BUY | 1.436 | 0.2 | 0.1 | -10.0 |
