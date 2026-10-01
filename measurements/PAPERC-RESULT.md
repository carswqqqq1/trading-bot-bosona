# Paper C

Public wallet `0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed` (@bosona). Paper fills only. No live orders, private keys, or Brez. Figures are from the runs in [the first log](paperc-2026-10-01.jsonl), [the next log](paperc-next-2026-10-01.jsonl), [the book-moving log](paperc-next2-2026-10-01.jsonl), [the exact-size log](paperc-size-2026-10-01.jsonl), [the following log](paperc-size2-2026-10-01.jsonl), [the fresh log](paperc-fresh-2026-10-01.jsonl), [the continued log](paperc-fresh2-2026-10-01.jsonl), [the next continued log](paperc-fresh3-2026-10-01.jsonl), [the following log](paperc-fresh4-2026-10-01.jsonl), [the next log](paperc-fresh5-2026-10-01.jsonl), [the following log](paperc-fresh6-2026-10-01.jsonl), [the next log](paperc-fresh7-2026-10-01.jsonl), [the following log](paperc-fresh8-2026-10-01.jsonl), [the next log](paperc-fresh9-2026-10-01.jsonl), [the following log](paperc-fresh10-2026-10-01.jsonl), and [the result file](paperc-result-2026-10-01.json).

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

## Better full-size price

His buy of 275.48695 Up shares of `btc-updown-5m-1790819700` at 0.08 was skipped while the log showed our price at 0.07. That 0.07 was a 5-share quote. The copy path capped the book at his tick, so his full size failed the cap and the 5-share price was written down as our price. A better price on his full size is a copy. The size rule and the sell rule were not changed. A book that cannot hold his exact size still does not invent the rest.

## Next sample

The book was not reset and the rules were not changed again. The run resumed at cash $11.08387458 with the three open positions still on. [Log](paperc-size2-2026-10-01.jsonl).

Two of those markets had a published resolution. The 404 from the earlier book quote was not used as a payout.

| Close | Shares | Public price | Proceeds | Cost | Realized on the close | Cash after | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Down `btc-updown-15m-1790819100` | 5 | 1 | 5 | 4.29463 | 0.70537 | 16.08387458 | none; this is not one of his fills |
| Down `btc-updown-5m-1790820000` | 13.888884 | 0 | 0 | 3.72575542 | -3.72575542 | 16.08387458 | none; this is not one of his fills |

Gamma `outcomePrices` were `["0", "1"]` for `["Up", "Down"]` on the 15-minute market, and `["1", "0"]` for `["Up", "Down"]` on the 5-minute market. Each close left unrealized P/L and the $78 check unset on that line because another position was still open. The mark taken immediately after both closes, with the hourly position still open, was unrealized P/L -$8.46365, equity $24.41950458. Equity did not reach $78.

Three 120-second windows then copied ten buys and no sells. Window 3 had no trade. Buys at a better full-size price were copied.

| Action | His size | Copied shares | Latency (seconds) | His price | Our price | Cent gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SKIP, full size worse than his price | 118.87 | 0 | 2.429 | 0.55 | 0.6 | 5.00 |
| SKIP, below 5 shares | 1.315787 | 0 | 1.401 | 0.2400000912 |  |  |
| SKIP, below 5 shares | 2.631579 | 0 | 1.623 | 0.2400000152 |  |  |
| SKIP, below 5 shares | 1.315787 | 0 | 1.816 | 0.2400000912 |  |  |
| PAPER_BUY | 5.27 | 5.27 | 1.47 | 0.8 | 0.57 | -23.00 |
| SKIP, full size worse than his price | 65.21739 | 0 | 1.685 | 0.3100000015 | 0.4545029998900599978011999560 | 14.45029983900599978011999560 |
| SKIP, below 5 shares | 1.449274 | 0 | 1.414 | 0.3100000414 |  |  |
| SKIP, full size worse than his price | 249 | 0 | 1.447 | 0.5524385542 | 0.5644160642570281124497991968 | 1.197751005702811244979919680 |
| PAPER_BUY | 5.55555 | 5.55555 | 6.014 | 0.1 | 0.02 | -8.00 |
| PAPER_BUY | 8.92 | 8.92 | 3.204 | 0.08 | 0.02 | -6.00 |
| PAPER_BUY | 5 | 5 | 3.433 | 0.08 | 0.02 | -6.00 |
| PAPER_BUY | 5 | 5 | 3.635 | 0.08 | 0.02 | -6.00 |
| PAPER_BUY | 5 | 5 | 3.869 | 0.08 | 0.02 | -6.00 |
| PAPER_BUY | 5 | 5 | 4.067 | 0.08 | 0.02 | -6.00 |
| PAPER_BUY | 52.33 | 52.33 | 4.29 | 0.06 | 0.01 | -5.00 |
| PAPER_BUY | 5 | 5 | 4.481 | 0.08 | 0.01 | -7.00 |
| PAPER_BUY | 5.319133 | 5.319133 | 4.677 | 0.0600000038 | 0.01 | -5.0000003800 |

End of window 1, before those buys, the only open position was still the hourly Down. Unrealized P/L was -$9.14183 and equity was $23.74132458. Equity did not reach $78.

| | USD |
| --- | ---: |
| Ending cash | 11.58283225 |
| Realized P/L | -6.11684542 |
| Unrealized P/L | not quoted |
| Equity | not quoted |
| Equity reached $78 | no quoted equity reached it |

The new 5-minute book returned HTTP 404, so the sample end did not mark one unrealized P/L or equity, and no payout was invented for that book.

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Down `bitcoin-up-or-down-september-30-2026-10pm-et` | 35.19 | 16.79928 | 10.72479 |
| Up `btc-updown-5m-1790820600` | 102.394683 | 4.50104233 | not quoted (HTTP 404) |

## Fresh $39 book

The book was steadily losing, so the sample that was still running was stopped. Cash was reset to $39 and the open positions were not carried forward. Exactly one rule changed: in the same minute a paper position opens, sell it when the bid is above paper cost, without waiting for a sell he prints. [Log](paperc-fresh-2026-10-01.jsonl).

Window 1 had no trade. Cash stayed $39, realized P/L $0, unrealized P/L $0, equity $39. Equity did not reach $78.

Three positions opened and closed in the minute they opened. Each close left no shares on the book.

| Close | Shares | Sell price | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Up `btc-updown-5m-1790821800` | 5.263158 | 0.83 | 4.11788166 | 0.19855948 | 39.19855948 | 0 | 39.19855948 | no | 9.561 |
| Down `btc-updown-15m-1790821800` | 6.0 | 0.4 | 2.25677 | 0.04243 | 39.24098948 | 0 | 39.24098948 | no | 19.209 |
| Down `bitcoin-up-or-down-september-30-2026-10pm-et` | 50.0 | 0.29 | 13.67340 | 0.10595 | 39.34693948 | 0 | 39.34693948 | no | 6.522 |

Those latencies are his opening fill to the paper sell. The sells are not sells he printed.

| | USD |
| --- | ---: |
| Ending cash | 39.34693948 |
| Realized P/L | 0.34693948 |
| Unrealized P/L | 0 |
| Equity | 39.34693948 |
| Equity reached $78 | no |

No position was left open. Latency in seconds from his fill to each paper action: 1.418, 2.531, 9.561, 1.403, 0.471, 0.664, 2.553, 2.744, 19.209, 2.428, 1.403, 6.522.

## Continued from $39.34693948

The book was not reset and no rule was changed. The run resumed at cash $39.34693948 with no open positions. A same-minute sell now reads the bid while his activity request is in flight, and it sells on the first book that clears paper cost. [Log](paperc-fresh2-2026-10-01.jsonl).

| Close | Shares | Sell price | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Up `btc-updown-15m-1790821800` | 31.45 | 0.12 | 3.34314 | 0.19838 | 39.54531948 | 0 | 39.54531948 | no | 11.315 |
| Down `btc-updown-5m-1790822400` | 38.07 | 0.31 | 11.19684 | 0.03484 | 39.58015948 | 0 | 39.58015948 | no | 3.487 |
| Up `btc-updown-15m-1790821800` | 6.67 | 0.15 | 0.84970 | 0.09127 | 39.67142948 | 0 | 39.67142948 | no | 6.169 |

Those latencies are his opening fill to the paper sell.

One position was still open at the sample end. Its market had not resolved, and the opening minute had passed, so it stayed open. The book quoted it.

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Up `btc-updown-5m-1790822400` | 40.19 | 26.82408 | 30.86544 |

| | USD |
| --- | ---: |
| Ending cash | 12.84734948 |
| Realized P/L | 0.67142948 |
| Unrealized P/L | 4.04136 |
| Equity | 43.71278948 |
| Equity reached $78 | no |

Latency in seconds from his fill to each paper action: 1.4, 1.429, 11.315, 1.422, 1.633, 2.509, 2.895, 3.487, 0.543, 0.941, 6.169, 1.406, 2.422.

## Continued from $12.84734948

The book was not reset and no rule was changed. The run resumed at cash $12.84734948 with 40.19 Up shares of `btc-updown-5m-1790822400` still open, cost $26.82408. [Log](paperc-fresh3-2026-10-01.jsonl).

At resume that position had no bid quote, so unrealized P/L was left unset. The resumed equity figure of $12.84734948 is cash only.

The market later published a resolution. Gamma `umaResolutionStatus` was `resolved`, the market was closed, and `outcomePrices` were `["1", "0"]` for outcomes `["Up", "Down"]`, `closedTime` `2026-10-01 02:46:26+00`. The paper close used the published Up price of 1.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| Up `btc-updown-5m-1790822400` | 40.19 | 1 | 40.19 | 26.82408 | 13.36592 | 50.46530948 | unset; the hourly position was still open | unset | unset on this close | none; this is not one of his fills |

The same windows copied one buy: 20.19 Up shares of `bitcoin-up-or-down-september-30-2026-10pm-et` at his price 0.12, gross $2.4228, fee $0.14924, cost $2.57204, cash after the buy $10.27530948, latency 1.421 seconds. That position was still open at the sample end. No same-minute sell printed.

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Up `bitcoin-up-or-down-september-30-2026-10pm-et` | 20.19 | 2.57204 | 2.84830 |

| | USD |
| --- | ---: |
| Ending cash | 50.46530948 |
| Realized P/L | 14.03734948 |
| Unrealized P/L | 0.27626 |
| Equity | 53.31360948 |
| Equity reached $78 | no |

Latency in seconds from his fill to each paper action: 1.413, 1.463, 1.471, 1.421, 1.451, 1.643, 6.189. The resolution has no fill-to-action latency.

## Continued from $50.46530948

The book was not reset and no rule was changed. The run resumed at cash $50.46530948 with 20.19 Up shares of `bitcoin-up-or-down-september-30-2026-10pm-et` still open, cost $2.57204. [Log](paperc-fresh4-2026-10-01.jsonl).

At resume the bid quote marked unrealized P/L at $2.01577 and equity at $55.05311948. Equity was under $78.

The hourly market had no published resolution. Gamma `umaResolutionStatus` was unset and `closed` was false, so the paper position stayed open. The live outcome prices on that open market were not used as a payout.

No position closed. Windows 1 and 2 had no trade. Window 3 skipped his buy of 25 Down shares of `btc-updown-15m-1790823600` at 0.44 because the full-size book price was 0.458. Latency 2.436 seconds.

| | Window 1 | Window 2 |
| --- | ---: | ---: |
| Cash | 50.46530948 | 50.46530948 |
| Realized P/L | 14.03734948 | 14.03734948 |
| Unrealized P/L | 8.99818 | -2.38413 |
| Equity | 62.03552948 | 50.65321948 |
| Equity reached $78 | no | no |

The sample-end book for the hourly position was stale, so unrealized P/L and equity were left unset then. The last quoted mark is the end of window 2.

| Open position | Shares | Cost |
| --- | ---: | ---: |
| Up `bitcoin-up-or-down-september-30-2026-10pm-et` | 20.19 | 2.57204 |

| | USD |
| --- | ---: |
| Ending cash | 50.46530948 |
| Realized P/L | 14.03734948 |
| Unrealized P/L at sample end | not quoted |
| Last quotable unrealized P/L (end of window 2) | -2.38413 |
| Last quotable equity (end of window 2) | 50.65321948 |
| Equity reached $78 | no |

Latency in seconds from his fill to the paper action: 2.436.

## Continued again from $50.46530948

The book was not reset and no rule was changed. The run resumed at cash $50.46530948 with 20.19 Up shares of `bitcoin-up-or-down-september-30-2026-10pm-et` still open, cost $2.57204. [Log](paperc-fresh5-2026-10-01.jsonl).

The hourly market still had no published resolution. A resolution requires Gamma `umaResolutionStatus` `resolved` and `closed` true. That market stayed open, so the paper position stayed open. Live outcome prices were not used as a payout.

No position closed. Nine buys copied his exact share count at a full-size price at or below his price. No same-minute sell printed.

| Buy | Shares | His price | Our price | Gross | Fee | Cash after | Latency (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Up `btc-updown-15m-1790823600` | 33 | 0.29 | 0.2312121212121212121212121212 | 7.630 | 0.41058 | 42.42472948 | 2.462 |
| Up `btc-updown-5m-1790823900` | 90 | 0.32 | 0.3027622222222222222222222222 | 27.2486 | 1.32979 | 13.84633948 | 2.914 |
| Up `btc-updown-5m-1790823900` | 5 | 0.32 | 0.3 | 1.50 | 0.07350 | 12.27283948 | 3.502 |
| Up `btc-updown-5m-1790823900` | 10.15625 | 0.36 | 0.31 | 3.1484375 | 0.15207 | 8.97233198 | 2.612 |
| Up `btc-updown-5m-1790823900` | 5 | 0.36 | 0.31 | 1.550 | 0.07487 | 7.34746198 | 1.444 |
| Up `btc-updown-5m-1790823900` | 5 | 0.36 | 0.31 | 1.550 | 0.07487 | 5.72259198 | 1.637 |
| Up `btc-updown-5m-1790823900` | 5 | 0.36 | 0.31 | 1.550 | 0.07487 | 4.09772198 | 1.833 |
| Up `btc-updown-5m-1790823900` | 5 | 0.36 | 0.31 | 1.550 | 0.07487 | 2.47285198 | 2.054 |
| Up `btc-updown-5m-1790823900` | 5 | 0.36 | 0.31 | 1.550 | 0.07487 | 0.84798198 | 2.246 |

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Up `bitcoin-up-or-down-september-30-2026-10pm-et` | 20.19 | 2.57204 | not quoted; stale book |
| Up `btc-updown-15m-1790823600` | 33 | 8.04058 | 13.19847 |
| Up `btc-updown-5m-1790823900` | 130.15625 | 41.5767475 | not quoted; stale book |

| | USD |
| --- | ---: |
| Ending cash | 0.84798198 |
| Realized P/L | 14.03734948 |
| Unrealized P/L | not quoted |
| Equity | not quoted |
| Equity reached $78 | not marked; two books were stale |

Latency in seconds from his fill to each paper action: 2.462, 2.914, 3.502, 2.438, 2.403, 2.612, 1.444, 1.637, 1.833, 2.054, 2.246, 2.447, 2.67, 2.87, 3.069, 3.262, 2.377, 2.218, 1.418, 2.4.

## Continued from $0.84798198

The book was not reset and no rule was changed. The run resumed at cash $0.84798198 with the three open positions still on, at the stored costs. [Log](paperc-fresh6-2026-10-01.jsonl).

Each close used the published Gamma `outcomePrices` after `umaResolutionStatus` was `resolved` and the market was closed. The stored cost was the cost already on the book.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Up `bitcoin-up-or-down-september-30-2026-10pm-et` | 20.19 | 0 | 0.00 | 2.57204 | -2.57204 | 0.84798198 | unset; two positions still open | unset | unset on this close | none |
| Up `btc-updown-5m-1790823900` | 130.15625 | 1 | 130.15625 | 41.5767475 | 88.5795025 | 131.00423198 | unset; the 15-minute position still open | unset | unset on this close | none |
| Up `btc-updown-15m-1790823600` | 33 | 0 | 0.0 | 8.04058 | -8.04058 | 131.00423198 | 0 | 131.00423198 | yes | none |

The first market published `["0", "1"]` for `["Up", "Down"]`. The 5-minute market published `["1", "0"]`. The 15-minute market published `["0", "1"]`. A public resolution is not one of his fills.

No buy was copied. Window 2 had no trade. Window 3 skipped ten of his buys because the full-size book was above his price or his size was under 5 shares. No same-minute sell printed. Nothing was left open.

| | USD |
| --- | ---: |
| Ending cash | 131.00423198 |
| Realized P/L | 92.00423198 |
| Unrealized P/L | 0 |
| Equity | 131.00423198 |
| Equity reached $78 | yes |

Latency in seconds from his fill to each paper action: 2.423, 2.635, 2.854, 1.522, 1.717, 1.919, 2.134, 1.568, 1.802, 2.029. The three resolutions have no fill-to-action latency.

## Continued from $131.00423198

The book was not reset and no rule was changed. The run resumed at cash $131.00423198 with nothing open. Cumulative realized P/L was $92.00423198. Equity at resume was $131.00423198. [Log](paperc-fresh7-2026-10-01.jsonl).

Ten buys copied his exact share count at a full-size price at or below his price. No same-minute sell printed. Window 2 had no trade.

| Buy | Shares | His price | Our price | Gross | Fee | Cash after | Latency (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Up `btc-updown-5m-1790824800` | 12.1 | 0.09 | 0.09 | 1.089 | 0.06937 | 129.84586198 | 0.418 |
| Up `btc-updown-15m-1790824500` | 36 | 0.22 | 0.22 | 7.920 | 0.43243 | 121.49343198 | 2.016 |
| Up `btc-updown-5m-1790824800` | 106.021266 | 0.0600000004 | 0.06 | 6.36127596 | 0.41857 | 114.71358602 | 1.93 |
| Up `btc-updown-5m-1790824800` | 58.36 | 0.06 | 0.06 | 3.5016 | 0.23041 | 110.98157602 | 2.71 |
| Up `btc-updown-5m-1790824800` | 84.601266 | 0.0600000005 | 0.06 | 5.07607596 | 0.33401 | 105.57149006 | 3.311 |
| Up `btc-updown-5m-1790824800` | 10 | 0.3 | 0.1 | 1.00 | 0.06300 | 104.50849006 | 1.451 |
| Up `btc-updown-5m-1790824800` | 9 | 0.31 | 0.09 | 0.810 | 0.05160 | 103.64689006 | 1.655 |
| Up `btc-updown-5m-1790824800` | 218 | 0.31 | 0.09 | 19.620 | 1.24979 | 82.77710006 | 1.847 |
| Up `btc-updown-5m-1790824800` | 5 | 0.31 | 0.09 | 0.450 | 0.02867 | 82.29843006 | 2.048 |
| Up `btc-updown-5m-1790824800` | 5 | 0.31 | 0.1 | 0.50 | 0.03150 | 81.76693006 | 2.45 |

The window-end checks left the 5-minute position open. Gamma later showed that market resolved and closed, `closedTime` `2026-10-01 03:25:54+00`, `outcomePrices` `["0", "1"]` for `["Up", "Down"]`. The paper close used the published Up price of 0 and the stored cost.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| Up `btc-updown-5m-1790824800` | 508.082532 | 0 | 0.000000 | 40.88487192 | -40.88487192 | 81.76693006 | unset; the 15-minute position still open | unset | unset on this close | none |

The 15-minute market had no published resolution. Its book was stale at the end, so unrealized P/L was left unset. The HTTP 404 on the 5-minute book was not used as a price.

| Open position | Shares | Cost |
| --- | ---: | ---: |
| Up `btc-updown-15m-1790824500` | 36 | 8.35243 |

| | USD |
| --- | ---: |
| Ending cash | 81.76693006 |
| Realized P/L | 51.11936006 |
| Unrealized P/L | not quoted |
| Equity | not quoted; cash is 81.76693006, so equity stays above $78 |
| Equity reached $78 | yes |

Latency in seconds from his fill to each paper action: 0.418, 2.016, 1.93, 2.71, 3.311, 1.422, 1.451, 1.655, 1.847, 2.048, 2.45, 2.661, 2.461, 1.466. The resolution has no fill-to-action latency.

## Continued from $81.76693006

The book was not reset and no rule was changed. The run resumed at cash $81.76693006 with 36 Up shares of `btc-updown-15m-1790824500` still open, cost $8.35243. Cumulative realized P/L was $51.11936006. [Log](paperc-fresh8-2026-10-01.jsonl).

That market published a resolution. Gamma `umaResolutionStatus` was `resolved`, the market was closed, and `outcomePrices` were `["1", "0"]` for `["Up", "Down"]`, `closedTime` `2026-10-01 03:30:56+00`. The paper close used the published Up price of 1 and the stored cost.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Up `btc-updown-15m-1790824500` | 36 | 1 | 36.0 | 8.35243 | 27.64757 | 117.76693006 | 0 | 117.76693006 | yes | none |

Two buys then copied his exact share count at a full-size price below his price. No same-minute sell printed.

| Buy | Shares | His price | Our price | Gross | Fee | Cash after | Latency (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Down `btc-updown-5m-1790825700` | 34.920635 | 0.3700000014 | 0.36 | 12.57142860 | 0.56320 | 104.63230146 | 2.476 |
| Down `bitcoin-up-or-down-september-30-2026-11pm-et` | 50 | 0.24 | 0.23 | 11.500 | 0.61985 | 92.51245146 | 2.832 |

The sample-end book could not mark both positions. The 5-minute book had insufficient depth, and that gap was not used as a price. The hourly position had a liquidation quote of $6.10415.

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Down `btc-updown-5m-1790825700` | 34.920635 | 13.13462860 | not quoted; insufficient depth |
| Down `bitcoin-up-or-down-september-30-2026-11pm-et` | 50 | 12.11985 | 6.10415 |

| | USD |
| --- | ---: |
| Ending cash | 92.51245146 |
| Realized P/L | 78.76693006 |
| Unrealized P/L | not quoted |
| Equity | not quoted; cash is 92.51245146, so equity stays above $78 |
| Equity reached $78 | yes |

Latency in seconds from his fill to each paper action: 3.429, 3.627, 3.83, 4.018, 1.448, 1.46, 1.411, 2.434, 2.476, 1.787, 2.832, 2.409, 2.411. The resolution has no fill-to-action latency.

## Continued from $92.51245146

The book was not reset and no rule was changed. The run resumed at cash $92.51245146 with both positions still open at the stored costs. Cumulative realized P/L was $78.76693006. [Log](paperc-fresh9-2026-10-01.jsonl).

The 5-minute market published a resolution. Gamma `umaResolutionStatus` was `resolved`, the market was closed, and `outcomePrices` were `["1", "0"]` for `["Up", "Down"]`, `closedTime` `2026-10-01 03:40:53+00`. The paper close used the published Down price of 0 and the stored cost, which includes the fee.

| Close | Shares | Public price | Proceeds | Cost | Realized | Cash after | Unrealized | Equity | Equity reached $78 | Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| Down `btc-updown-5m-1790825700` | 34.920635 | 0 | 0.000000 | 13.13462860 | -13.13462860 | 92.51245146 | unset; the hourly position still open | unset | unset on this close | none |

The hourly market had no published resolution. Its bid quote is a mark, not a payout.

| Open position | Shares | Cost | Liquidation quote |
| --- | ---: | ---: | ---: |
| Down `bitcoin-up-or-down-september-30-2026-11pm-et` | 50 | 12.11985 | 0.83813 |

No buy was copied. Windows 2 and 3 had no trade. One skip was his buy of 3.06 Up shares of `btc-updown-5m-1790826000` at 0.47, under the 5-share minimum. Latency 2.437 seconds. No same-minute sell printed.

| | USD |
| --- | ---: |
| Ending cash | 92.51245146 |
| Realized P/L | 65.63230146 |
| Unrealized P/L | -11.28172 |
| Equity | 93.35058146 |
| Equity reached $78 | yes |

Latency in seconds from his fill to the paper action: 2.437. The resolution has no fill-to-action latency.

## Continued from $92.51245146 again

The book was not reset and no rule was changed. The run resumed at cash $92.51245146 with 50 Down shares of `bitcoin-up-or-down-september-30-2026-11pm-et` still open, cost $12.11985. Cumulative realized P/L was $65.63230146. [Log](paperc-fresh10-2026-10-01.jsonl).

The hourly market had no published resolution. Gamma `umaResolutionStatus` was unset and `closed` was false. Live outcome prices were not used as a payout. The position stayed open.

No position closed. No buy was copied. All three windows had no trade. No same-minute sell printed.

At resume the bid quote marked unrealized P/L at -$11.80428 and equity at $92.82802146. The end of window 1 marked unrealized P/L at -$12.02684 and equity at $92.60546146. The sample-end book was stale, so unrealized P/L and equity were left unset then.

| Open position | Shares | Cost |
| --- | ---: | ---: |
| Down `bitcoin-up-or-down-september-30-2026-11pm-et` | 50 | 12.11985 |

| | USD |
| --- | ---: |
| Ending cash | 92.51245146 |
| Realized P/L | 65.63230146 |
| Unrealized P/L at sample end | not quoted |
| Last quotable unrealized P/L (end of window 1) | -12.02684 |
| Last quotable equity (end of window 1) | 92.60546146 |
| Equity reached $78 | yes |

There was no fill of his in these windows, so there is no fill-to-action latency.
