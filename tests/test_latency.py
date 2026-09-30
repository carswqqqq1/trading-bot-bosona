import unittest

import latency
from bot import row_keys


WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40


def config():
    return {"leader_wallet": WALLET, "timeframes_minutes": [5, 15, 60]}


def row(**overrides):
    result = {
        "proxy_wallet": WALLET,
        "transaction_hash": "0xtx",
        "condition_id": "0xcondition",
        "token_id": "12345",
        "timestamp": 1000,
        "side": "BUY",
        "size": "10",
        "price": "0.50",
        "usdc_size": "5",
        "type": "TRADE",
        "slug": "btc-updown-5m-999",
        "outcome": "Up",
        "is_combo": False,
    }
    result.update(overrides)
    return result


class LatencyObservationTests(unittest.TestCase):
    def test_first_seen_buy_and_sell_report_source_delays(self):
        buy = row()
        sell = row(side="SELL", transaction_hash="0xsell", timestamp=995)
        result = latency.observe([buy, sell], set(), config(), 1012, 0.125)
        self.assertEqual([sample["side"] for sample in result], ["BUY", "SELL"])
        self.assertEqual([sample["source_to_first_seen_seconds"] for sample in result], [12, 17])
        self.assertTrue(all(sample["valid_clock_sample"] for sample in result))
        self.assertTrue(all(sample["executed"] is False for sample in result))

    def test_baseline_excludes_existing_rows_and_identical_fill_multiplicity_is_seen_once(self):
        old = row(transaction_hash="0xold")
        twin = row(transaction_hash="0xtwin")
        seen = {key for key, _ in row_keys([old, twin])}
        snapshot = [old, twin, twin]
        first = latency.observe(snapshot, seen, config(), 1010, 0.1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["source_transaction"], "0xtwin")
        second = latency.observe(snapshot, seen, config(), 1011, 0.1)
        self.assertEqual(second, [])

    def test_other_wallet_and_non_btc_rows_are_filtered(self):
        result = latency.observe(
            [row(proxy_wallet=OTHER), row(transaction_hash="0xother", slug="eth-updown-5m-999")],
            set(), config(), 1010, 0.1,
        )
        self.assertEqual(result, [])

    def test_summary_handles_no_samples_and_reports_delay_statistics(self):
        empty = latency.summary([], 4.567)
        self.assertEqual(empty["status"], "SUMMARY")
        self.assertEqual(empty["duration_seconds"], 4.57)
        self.assertEqual(empty["valid_delay_samples"], 0)
        self.assertIn("cannot be estimated", empty["note"])

        observations = [
            {"side": "BUY", "source_to_first_seen_seconds": delay, "valid_clock_sample": True}
            for delay in (1, 2, 3, 4)
        ] + [{"side": "SELL", "source_to_first_seen_seconds": -1, "valid_clock_sample": False}]
        summary = latency.summary(observations, 10)
        self.assertEqual(summary["new_btc_trades"], 5)
        self.assertEqual(summary["valid_delay_samples"], 4)
        self.assertEqual(summary["buys"], 4)
        self.assertEqual(summary["sells"], 1)
        self.assertEqual(summary["min_seconds"], 1)
        self.assertEqual(summary["max_seconds"], 4)
        self.assertEqual(summary["median_seconds"], 2.5)
        self.assertEqual(summary["p95_seconds"], 4)


if __name__ == "__main__":
    unittest.main()
