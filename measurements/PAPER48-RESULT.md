# $48 public-data paper validation

Completed 2026-09-30. [Raw log](paper48-validation-2026-09-30.jsonl) and
[structured results](paper48-result-2026-09-30.json) preserve three short runs.
No real money, wallet credentials, or Future orders were used.

| Final measure | USD |
| --- | ---: |
| Starting paper capital | 48.00 |
| Cash | 43.20320 |
| Open positions at fee-adjusted bid liquidation quotes | 7.85036 |
| Quoted total equity | 51.05356 |
| Change from start, unrealized | +3.05356 |
| Simulated BUY fees paid | 0.25120 |
| Realized PnL | 0.00 |

Two BUYs were simulated; no SELLs filled. BUY/outcome limits stayed $2.40 and
total open cost stayed below $9.60. Final open cost was $4.79680. A liquidation
quote is not a realized sale. There was no settlement or redemption simulation.
Prices can change immediately after this snapshot.

## What the captures establish

The first 120-second run saw no new qualifying trades and finished at $48.
The next 300-second run recorded 19 source observations: one old restart
signal was stale, and 18 BUYs were skipped because minimum size could not fit
the $2.40 cap. Cash again remained $48.

The final 90-second capture recorded eight observations, including five
pre-start backlog events and three continuously observed BUYs. Two paper
BUYs filled. Continuous source-to-observation delays ranged from 1.263 to
1.503 seconds. Earlier paired monitoring matched eight BUYs and reduced median
detection delay from 2.154 to 1.611 seconds.

## Fresh copy price disadvantage

The continuously observed hourly BUY was seen after 1.409 seconds and simulated
after 1.761 seconds. Bosona's source price was $0.10; the simulated ask VWAP
was $0.12 for 18.83 shares. The same-quantity price disadvantage was $0.37660.
The additional hypothetical taker fee at the copied price was $0.02056, for a
combined disadvantage of **$0.39716** versus the same shares at the source price.
Bosona's actual maker/taker fee role is unknown. Spread, liquidity, size, and
price movement all contribute; this does not isolate causal latency loss.

## Restart caveat and subsequent fix

The other paper BUY came from a 17.391-second-old restart signal, buying 7.62
shares at $0.30 versus a source price near $0.51. That favorable difference
accounts for much of the comparison advantage and is not representative of
normal fast detection. It was permitted by the then-current 20-second signal
age cap. The current runner skips trades predating the current run while
tracking their source inventory; a regression test verifies no cash is spent.

Thus **the +$3.05 snapshot does not establish profitability of fast copying**.
There were no live SELL samples, no Future execution, and too few fills for a
strategy conclusion. A further clean run should use the current code and a
fresh database, with the same sizing unless a different paper policy is chosen.
