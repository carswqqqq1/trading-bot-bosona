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
