"""Measure first-seen public BTC BUY/SELL activity; never submit orders."""
import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from bot import RateLimited, activity, row_keys, market_timeframe, validate


def observe(rows, seen, config, received_at, request_seconds):
    observations = []
    for key, row in row_keys(rows):
        if key in seen:
            continue
        seen.add(key)
        if (row.get("proxy_wallet", "").lower() != config["leader_wallet"].lower()
                or row.get("type") != "TRADE" or row.get("is_combo")
                or row.get("side") not in ("BUY", "SELL")
                or market_timeframe(row.get("slug", "")) not in config["timeframes_minutes"]):
            continue
        source = int(row["timestamp"])
        delay = received_at - source
        observations.append({
            "status": "OBSERVED", "side": row["side"], "outcome": row.get("outcome"),
            "slug": row["slug"], "source_transaction": row["transaction_hash"],
            "source_event_key": key,
            "source_timestamp_seconds": source,
            "first_seen_at_utc": datetime.fromtimestamp(received_at, timezone.utc).isoformat(),
            "source_to_first_seen_seconds": round(delay, 3),
            "activity_request_seconds": round(request_seconds, 3),
            "valid_clock_sample": delay >= 0,
            "executed": False,
        })
    return observations


def summary(observations, elapsed):
    delays = sorted(x["source_to_first_seen_seconds"] for x in observations
                    if x["valid_clock_sample"])
    result = {"status": "SUMMARY", "duration_seconds": round(elapsed, 2),
              "new_btc_trades": len(observations), "valid_delay_samples": len(delays),
              "buys": sum(x["side"] == "BUY" for x in observations),
              "sells": sum(x["side"] == "SELL" for x in observations),
              "execution_latency_measured": False}
    if delays:
        result.update(min_seconds=delays[0], max_seconds=delays[-1],
                      median_seconds=(delays[(len(delays)-1)//2] + delays[len(delays)//2])/2,
                      p95_seconds=delays[math.ceil(len(delays)*0.95)-1])
    else:
        result["note"] = "No new valid BTC trade observations; latency cannot be estimated."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.bosona.paper.json")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--output", default="latency.jsonl")
    parser.add_argument("--poll-seconds", type=float, default=None,
                        help="Override polling interval; minimum 0.25 seconds")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be positive and finite")
    config = validate(json.loads(Path(args.config).read_text()))
    if args.poll_seconds is not None:
        if not math.isfinite(args.poll_seconds) or args.poll_seconds < 0.25:
            parser.error("--poll-seconds must be finite and at least 0.25")
        config["poll_seconds"] = args.poll_seconds
    seen, observations = set(), []
    started = time.monotonic()
    # Fixed lookback start makes identical-row multiplicity stable across polls.
    # Baseline excludes trades that were already present when monitoring began.
    source_start = int(time.time()) - 3600
    baseline = activity(config["leader_wallet"], source_start, int(time.time()))
    seen.update(key for key, _ in row_keys(baseline))
    with Path(args.output).open("w") as output:
        def emit(record):
            line = json.dumps(record)
            output.write(line + "\n")
            output.flush()
            print(line, flush=True)
        emit({"status": "STARTED", "leader_wallet": config["leader_wallet"],
              "baseline_rows_excluded": len(baseline),
              "poll_seconds": config["poll_seconds"],
              "incremental_lookback_seconds": 120,
              "timestamp_precision_seconds": 1,
              "measurement": "source block timestamp to first successful public observation"})
        try:
            next_poll = time.monotonic() + config["poll_seconds"]
            while time.monotonic() - started < args.duration:
                remaining = args.duration - (time.monotonic() - started)
                time.sleep(min(max(0, next_poll - time.monotonic()), max(0, remaining)))
                if time.monotonic() - started >= args.duration:
                    break
                request_started = time.monotonic()
                next_poll = request_started + config["poll_seconds"]
                try:
                    end = int(time.time())
                    rows = activity(config["leader_wallet"], max(source_start, end - 120), end)
                except RateLimited as exc:
                    emit({"status": "ERROR", "message": str(exc), "retry_after_seconds": exc.retry_after})
                    next_poll = time.monotonic() + exc.retry_after
                    continue
                except Exception as exc:
                    emit({"status": "ERROR", "message": str(exc)})
                    next_poll = time.monotonic() + max(2, config["poll_seconds"])
                    continue
                received_at = time.time()
                samples = observe(rows, seen, config, received_at,
                                  time.monotonic() - request_started)
                observations.extend(samples)
                for sample in samples:
                    emit(sample)
        except KeyboardInterrupt:
            pass
        emit(summary(observations, time.monotonic() - started))


if __name__ == "__main__":
    main()
