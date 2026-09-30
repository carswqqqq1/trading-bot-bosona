"""Compare first-seen times for the same unambiguous trades in two live runs."""
import argparse
import json
from collections import defaultdict
from statistics import median


def load(path):
    grouped = defaultdict(list)
    with open(path) as source:
        for line in source:
            row = json.loads(line)
            if row.get("status") != "OBSERVED" or not row.get("valid_clock_sample"):
                continue
            identity = (row["source_transaction"], row["slug"], row["side"],
                        row.get("outcome"), row["source_timestamp_seconds"])
            grouped[identity].append(row)
    return grouped


def compare(baseline, faster):
    pairs = []
    for key in baseline.keys() & faster.keys():
        if len(baseline[key]) != 1 or len(faster[key]) != 1:
            continue
        old, new = baseline[key][0], faster[key][0]
        pairs.append({"source_transaction": key[0], "side": key[2],
                      "baseline_delay_seconds": old["source_to_first_seen_seconds"],
                      "faster_delay_seconds": new["source_to_first_seen_seconds"],
                      "earlier_detection_seconds": round(
                          old["source_to_first_seen_seconds"] - new["source_to_first_seen_seconds"], 3)})
    result = {"matched_unambiguous_trades": len(pairs), "pairs": pairs,
              "measured_execution_latency": False}
    if pairs:
        result.update(
            baseline_median_seconds=median(x["baseline_delay_seconds"] for x in pairs),
            faster_median_seconds=median(x["faster_delay_seconds"] for x in pairs),
            median_earlier_detection_seconds=median(x["earlier_detection_seconds"] for x in pairs))
    else:
        result["note"] = "No comparable trades; speed improvement cannot be estimated."
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline")
    parser.add_argument("faster")
    args = parser.parse_args()
    print(json.dumps(compare(load(args.baseline), load(args.faster)), indent=2))
