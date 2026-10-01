# Paper C

Public wallet `0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed` (@bosona). Paper fills only. No live orders, private keys, or Brez. Figures are from the runs in [the first log](paperc-2026-10-01.jsonl), [the next log](paperc-next-2026-10-01.jsonl), [the book-moving log](paperc-next2-2026-10-01.jsonl), [the exact-size log](paperc-size-2026-10-01.jsonl), and [the result file](paperc-result-2026-10-01.json).

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

## Book already moved

The exact-size rule started after another sample had already run. That sample was not reset. It resumed at cash $34.08600 with the 15 Up shares of `btc-updown-15m-1790818200`, cost $1.85721. [Log](paperc-next2-2026-10-01.jsonl).

During window 2 that market resolved. Gamma `outcomePrices` were `["0", "1"]` for `["Up", "Down"]`. The paper close used the published Up price of 0.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Up | 15 | 0 | 0 | 1.85721 | -1.85721 | 34.08600 | 0 | 34.08600 | no | none; this is not one of his fills |

Window 3 then copied two buys under the old 5-share clip and skipped two. No sell was copied. No rule was changed.

| Action | His size | Copied shares | Latency (seconds) | His price | Our price | Cent gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PAPER_BUY | 33 | 5 | 2.531 | 0.77 | 0.62 | -15.00 |
| SKIP, five shares above the buy budget | 465 | 0 | 2.728 | 0.77 | 0.62 | -15.00 |
| PAPER_BUY | 117 | 5 | 2.926 | 0.87 | 0.85 | -2.00 |
| SKIP, five shares above the buy budget | 1 | 0 | 1.47 | 0.61 | 0.58 | -3.00 |

| | USD |
| --- | ---: |
| Ending cash | 26.60891 |
| Realized P/L | -4.91400 |
| Unrealized P/L | 0.72525 |
| Equity | 34.81125 |
| Equity reached $78 | no |

Open at the end of that sample: 5 Down shares of `btc-updown-15m-1790819100`, cost $4.29463, and 5 Down shares of `btc-updown-5m-1790819400`, cost $3.18246.

## Exact share count

The next sample did not reset that book and did not drop those positions. A copied buy is now his exact share count, same side and same market, only when the book fills that size at his price or better. Below 5 shares is skipped. If that size costs more than the cash on hand, the buy is skipped and is not scaled down. The sell rule is unchanged. [Log](paperc-size-2026-10-01.jsonl).

Resumed cash $26.60891. Realized P/L at the resume was -$4.91400. The open books could not be quoted then, so unrealized P/L was not quoted at resume.

Three 120-second windows. Three buys copied his size. No sell was copied. No rule was changed.

| Action | His size | Copied shares | Latency (seconds) | His price | Our price | Cent gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SKIP, full size not at his price or better | 275.48695 | 0 | 1.412 | 0.08 | 0.07 | -1.00 |
| PAPER_BUY | 35.19 | 35.19 | 1.426 | 0.46 | 0.46 | 0.00 |
| SKIP, full size not at his price or better | 15 | 0 | 1.466 | 0.4 | 0.43 | 3.00 |
| PAPER_BUY | 6.944442 | 6.944442 | 1.434 | 0.2800000346 | 0.28 | -0.0000034600 |
| PAPER_BUY | 6.944442 | 6.944442 | 1.466 | 0.2800000346 | 0.23 | -5.0000034600 |
| SKIP, his size costs more than cash | 13.88 | 0 | 1.419 | 0.85 | 0.82 | -3.00 |
| SKIP, full size not at his price or better | 142.525424 | 0 | 1.421 | 0.4100000011 | 0.41 | -0.0000001100 |

At the end of window 2 the earlier 5 Down shares of `btc-updown-5m-1790819400` resolved. Gamma `outcomePrices` were `["0", "1"]` for `["Up", "Down"]`.

| Close | Shares | Public price | Proceeds | Cost | Realized on the close | Cash after | Unrealized | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| Down | 5 | 1 | 5 | 3.18246 | 1.81754 | 11.08387458 | not quoted; other positions stayed open | not set on the close, because other shares stayed open | none; this is not one of his fills |

| | USD |
| --- | ---: |
| Ending cash | 11.08387458 |
| Realized P/L | -3.09646 |
| Unrealized P/L | not quoted |
| Equity | not quoted |
| Equity reached $78 | no |

One open book returned HTTP 404, so the sample did not mark a single unrealized P/L or equity. The other two positions were quoted at the sample end:

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Down `btc-updown-15m-1790819100` | 5 | 4.29463 | not quoted (HTTP 404) |
| Down `bitcoin-up-or-down-september-30-2026-10pm-et` | 35.19 | 16.79928 | 16.27636 |
| Down `btc-updown-5m-1790820000` | 13.888884 | 3.72575542 | 7.39826620 |

Cash plus those two quotes is $34.75850078. Adding a $1 payout on the unquoted 5 shares, the highest payout a resolved share can pay, is $39.75850078. That is still under $78. Goal $78 was not reached.
