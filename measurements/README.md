# First public observation sample

The [JSONL log](bosona-2026-09-30.jsonl) contains a 60.45-second run on 2026-09-30
watching Bosona's public trading wallet with a two-second polling interval.
Existing rows were excluded at startup.

Three new BTC 5-minute BUY trades were observed. Source-to-first-seen delays
were 3.029, 2.029, and 2.029 seconds. Median: 2.029 seconds; maximum and p95:
3.029 seconds. No SELL trades were observed in this short sample.

These values measure source block timestamps to public observation, with
one-second source timestamp precision. They include indexing, polling, and
HTTP request time. Three trades in one short period are insufficient to
estimate typical latency or SELL latency. No follower order was placed, and
Future execution latency was not measured.

## Faster polling comparison

The [paired comparison](latency-comparison-2026-09-30.json) matches the same
eight unambiguous BUY trades from simultaneous public monitors. Median
source-to-observation delay was 2.154 seconds for the original two-second
polling loop versus 1.611 seconds for a 0.5-second request schedule with a
120-second incremental activity window. Median earlier detection: 0.543 seconds.

[Baseline capture](latency-baseline-2026-09-30.jsonl) and
[faster capture](latency-fast-2026-09-30.jsonl) preserve the raw observations.
The baseline capture is a snapshot of a longer running monitor. Comparisons
exclude transactions with ambiguous repeated records. No SELL samples were
captured in the three-minute faster run, and no Future order was submitted.
