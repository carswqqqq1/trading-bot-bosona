# Set up later

## Paper measurement

1. Install Python 3.10+ and clone this repository.
2. Run `python -m unittest discover -s tests -v` and `python demo.py`.
3. Run `python latency.py --config config.bosona.paper.json --duration 1800 --output latency-session-1.jsonl`.
4. Review BUY/SELL samples, source ages, errors, and the final median/p95 summary.
   Repeat during active trading if the run produces no new trades.
5. Select the allowed BTC timeframes and latency tolerance from observed results.

For the $48 cash-and-inventory simulation, run:

```bash
python paper.py --config config.paper48.json --duration 120 --db paper48.sqlite3 --output paper48.jsonl
```

The chosen caps are $2.40 per BUY/outcome and $9.60 total open cost, including
simulated fees. The runner watches both BUYs and SELLs with a 0.5-second target
poll interval, and simulates current executable depth when constraints permit.
Read the final `PORTFOLIO` record; unresolved holdings prevent a total PnL
estimate. An account API key is unnecessary for this paper stage.
Use a fresh database for a clean test of the current runner: older validation
captures include a restart trade that the corrected runner now skips.
See [recorded $48 results](measurements/PAPER48-RESULT.md).

No wallet connection or funding is required for this stage. Example trade
amounts in the config are paper values. The measurement observes public
activity and cannot submit orders.

## Future execution integration

The execution connector remains to be implemented after verifying a supported
route into the exact Future wallet. See [integration findings](FUTURE-INTEGRATION.md).
Information needed: public Future trading wallet, wallet type and supported
signer method, official API/account integration guidance, desired BUY sizing,
daily exposure/loss caps, and SELL behavior.

Before live copying, connect the tested paper inventory logic to bounded live BUY and SELL orders,
fees, durable order IDs and timeout reconciliation, fill/settlement tracking,
position limits, and an emergency stop. Prove that externally placed orders
appear in the intended Future account. A generic separate Polymarket wallet
does not fulfill the destination requirement.

Store secrets locally or in a deployment secret manager, never in chat, config
commits, or measurement logs. No live trading process is installed or running.
