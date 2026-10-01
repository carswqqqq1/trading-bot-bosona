import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import paper


NOW = 1_800_000_000
START = NOW - 30
WALLET = "0x" + "a" * 40
CONDITION = "0xcondition"
TOKEN_UP = "12345678901234567890"
TOKEN_DOWN = "98765432109876543210"


def config(**overrides):
    result = {
        "leader_wallet": WALLET,
        "timeframes_minutes": [5, 15],
        "copy_ratio": "0.05",
        "max_trade_usd": "2.40",
        "max_daily_proposed_usd": "48",
        "max_price_drift": "0.02",
        "max_signal_age_seconds": 20,
        "min_seconds_to_expiry": 5,
        "poll_seconds": 1,
        "starting_cash_usd": "48",
        "max_buy_usd": "5.00",
        "max_open_cost_usd": "9.60",
        "max_outcome_cost_usd": "5.00",
        "max_book_age_seconds": 5,
    }
    result.update(overrides)
    return result


def row(**overrides):
    result = {
        "proxy_wallet": WALLET,
        "transaction_hash": "0xtx",
        "condition_id": CONDITION,
        "token_id": TOKEN_UP,
        "timestamp": NOW - 1,
        "side": "BUY",
        "size": "100",
        "price": "0.50",
        "usdc_size": "50",
        "type": "TRADE",
        "slug": f"btc-updown-5m-{START}",
        "outcome": "Up",
        "is_combo": False,
    }
    result.update(overrides)
    return result


def market(**overrides):
    result = {
        "slug": f"btc-updown-5m-{START}",
        "conditionId": CONDITION,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "archived": False,
        "endDate": datetime.fromtimestamp(START + 300, timezone.utc).isoformat().replace("+00:00", "Z"),
        "clobTokenIds": [TOKEN_UP, TOKEN_DOWN],
        "outcomes": ["Up", "Down"],
        "feesEnabled": True,
        "feeSchedule": {"exponent": 1, "takerOnly": True, "rate": "0.1"},
    }
    result.update(overrides)
    return result


def book(**overrides):
    result = {
        "asset_id": TOKEN_UP,
        "market": CONDITION,
        "timestamp": NOW * 1000,
        "tick_size": "0.01",
        "min_order_size": "1",
        "asks": [{"price": "0.50", "size": "100"}, {"price": "0.51", "size": "100"}],
        "bids": [{"price": "0.49", "size": "100"}],
    }
    result.update(overrides)
    return result


class PaperJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.config = config()
        self.journal = paper.PaperJournal(self.path, self.config)

    def test_buy_charges_known_fee_and_costs_against_cash(self):
        result = self.journal.process("buy-1", row(price="0.50"), market(), book(), NOW)
        self.assertEqual(result["status"], "PAPER_BUY")
        shares = Decimal(result["shares"])
        gross = Decimal(result["gross"])
        fee = Decimal(result["fee"])
        self.assertEqual(fee, (shares * Decimal("0.1") * Decimal("0.5") * Decimal("0.5")).quantize(Decimal("0.00001")))
        self.assertEqual(Decimal(result["cash_usd"]), Decimal("48") - gross - fee)
        holding = self.journal.holdings()[TOKEN_UP]
        self.assertEqual(holding["shares"], shares)
        self.assertEqual(holding["cost"], gross + fee)
        self.assertLessEqual(holding["cost"], Decimal(self.config["max_buy_usd"]))
        self.assertEqual(Decimal(result["source_price"]), Decimal("0.50"))
        self.assertEqual(Decimal(result["simulated_vwap"]), Decimal("0.50"))
        self.assertEqual(Decimal(result["his_price"]), Decimal("0.50"))
        self.assertEqual(Decimal(result["our_price"]), Decimal("0.50"))
        self.assertEqual(Decimal(result["cent_difference"]), Decimal("0"))
        self.assertEqual(Decimal(result["source_price_slippage_cost_usd"]), Decimal("0"))
        source_fee = (shares * Decimal("0.1") * Decimal("0.50") * Decimal("0.50")).quantize(Decimal("0.00001"))
        self.assertEqual(Decimal(result["source_price_fee_estimate_usd"]), source_fee)
        self.assertEqual(Decimal(result["fee_delta_vs_hypothetical_source_price_usd"]), fee - source_fee)

    def test_sell_uses_follower_inventory_and_nets_sell_fee(self):
        bought = self.journal.process("buy-1", row(), market(), book(), NOW)
        before_cash = self.journal.cash
        before_shares = Decimal(bought["shares"])
        before_cost = Decimal(bought["gross"]) + Decimal(bought["fee"])
        result = self.journal.process(
            "sell-1", row(transaction_hash="0xsell", side="SELL", size="25", price="0.60"),
            market(), book(bids=[{"price": "0.60", "size": "100"}]), NOW,
        )
        self.assertEqual(result["status"], "PAPER_SELL")
        sold_shares = Decimal(result["shares"])
        self.assertEqual(sold_shares, (before_shares * Decimal("0.25")).quantize(Decimal("0.01")))
        self.assertLessEqual(sold_shares, before_shares)
        self.assertEqual(self.journal.cash, before_cash + Decimal(result["gross"]) - Decimal(result["fee"]))
        holding = self.journal.holdings()[TOKEN_UP]
        self.assertGreaterEqual(holding["shares"], 0)
        expected_remaining_cost = before_cost * (before_shares - sold_shares) / before_shares
        self.assertEqual(holding["cost"], expected_remaining_cost)
        self.assertEqual(Decimal(result["realized_pnl_usd"]), Decimal(result["gross"]) - Decimal(result["fee"]) - (before_cost - expected_remaining_cost))
        self.assertGreaterEqual(self.journal.cash, 0)

    def test_unmatched_source_sell_cannot_oversell_follower_position(self):
        result = self.journal.process(
            "sell-without-buy", row(side="SELL", size="1"), market(), book(), NOW,
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertIn("unmatched_source_sell", result["reason"])
        self.assertEqual(self.journal.cash, Decimal("48"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)

    def test_duplicate_key_after_restart_does_not_copy_or_charge_again(self):
        first = self.journal.process("buy-1", row(), market(), book(), NOW)
        cash_after_buy = self.journal.cash
        self.journal.db.close()
        restarted = paper.PaperJournal(self.path, self.config)
        self.addCleanup(restarted.db.close)
        duplicate = restarted.process("buy-1", row(), market(), book(), NOW)
        self.assertEqual(duplicate["status"], "DUPLICATE")
        self.assertEqual(restarted.cash, cash_after_buy)
        self.assertEqual(restarted.holdings()[TOKEN_UP]["shares"], Decimal(first["shares"]))

    def test_resume_backlog_tracks_source_without_copying_old_trade(self):
        result=self.journal.process("backlog",row(),None,None,NOW,skip_reason="resume_backlog_not_copied")
        self.assertEqual(result["status"],"SKIP")
        self.assertEqual(result["reason"],"resume_backlog_not_copied")
        self.assertEqual(self.journal.cash,Decimal("48"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["leader_shares"],Decimal("100"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"],0)

    def test_market_minimum_causes_skip_without_exceeding_cash_or_cost_cap(self):
        tight = config(max_buy_usd="2.40", max_outcome_cost_usd="2.40")
        journal = paper.PaperJournal(Path(self.tmp.name) / "tight.sqlite", tight)
        self.addCleanup(journal.db.close)
        constrained = book(min_order_size="5")
        result = journal.process("buy-too-small", row(), market(), constrained, NOW)
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "five_shares_exceed_per_buy_budget")
        self.assertTrue(result["rule_skipped"])
        self.assertEqual(journal.cash, Decimal("48"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["cost"], 0)

    def test_open_cost_cap_and_cash_are_never_exceeded(self):
        self.config["max_open_cost_usd"] = "3.00"
        # Journal config is fixed at creation, so use a fresh journal for this policy.
        capped = paper.PaperJournal(Path(self.tmp.name) / "capped.sqlite", self.config)
        self.addCleanup(capped.db.close)
        first = capped.process("buy-1", row(), market(), book(), NOW)
        # Distinct outcome token and condition exercise portfolio open-cost cap.
        other_row = row(transaction_hash="0xother", condition_id="0xothercondition", token_id="33333")
        other_market = market(conditionId="0xothercondition", clobTokenIds=["33333", TOKEN_DOWN])
        other_book = book(asset_id="33333", market="0xothercondition")
        second = capped.process("buy-2", other_row, other_market, other_book, NOW)
        self.assertEqual(first["status"], "PAPER_BUY")
        self.assertEqual(second["status"], "SKIP")
        self.assertGreaterEqual(capped.cash, 0)
        self.assertLessEqual(sum((p["cost"] for p in capped.holdings().values()), Decimal(0)), Decimal("3.00"))
        self.assertEqual(second["reason"], "open_risk_cap")

    def test_optional_minimum_size_allowance_buys_only_required_quantity(self):
        policy = config(target_buy_usd="2.40",max_buy_usd="4.80",max_outcome_cost_usd="4.80")
        journal = paper.PaperJournal(Path(self.tmp.name)/"minimum.sqlite",policy)
        self.addCleanup(journal.db.close)
        result = journal.process("minimum",row(price="0.70"),market(),
                                 book(min_order_size="5",asks=[{"price":"0.70","size":"100"}],
                                      bids=[{"price":"0.69","size":"100"}]),NOW)
        self.assertEqual(result["status"],"PAPER_BUY")
        self.assertEqual(Decimal(result["shares"]),Decimal("5"))
        self.assertLessEqual(Decimal(result["gross"])+Decimal(result["fee"]),Decimal("4.80"))

    def test_minimum_allowance_cannot_exceed_hard_cap(self):
        policy = config(target_buy_usd="2.40",max_buy_usd="4.80",max_outcome_cost_usd="4.80")
        journal = paper.PaperJournal(Path(self.tmp.name)/"unaffordable.sqlite",policy)
        self.addCleanup(journal.db.close)
        result = journal.process("expensive",row(price="0.97"),market(),
                                 book(min_order_size="5",asks=[{"price":"0.97","size":"100"}],
                                      bids=[{"price":"0.96","size":"100"}]),NOW)
        self.assertEqual(result["status"],"SKIP")
        self.assertEqual(journal.cash,Decimal("48"))
        self.assertFalse(result["minimum_size_price_comparison"]["executed"])

    def test_stale_book_and_closed_market_are_skipped_without_debit(self):
        stale = self.journal.process("stale", row(), market(), book(timestamp=(NOW - 6) * 1000), NOW)
        closed = self.journal.process("closed", row(transaction_hash="0xclosed"), market(closed=True), book(), NOW)
        self.assertEqual(stale["status"], "SKIP")
        self.assertEqual(stale["reason"], "stale_or_future_book")
        self.assertEqual(closed["status"], "SKIP")
        self.assertEqual(closed["reason"], "market_not_tradable")
        self.assertEqual(self.journal.cash, Decimal("48"))

    def test_portfolio_marks_open_position_at_bid_and_reports_pnl(self):
        bought = self.journal.process("buy-1", row(), market(), book(), NOW)
        fetch_calls = []

        def fetch(url, params=None):
            fetch_calls.append(url)
            if "/markets/slug/" in url:
                return market()
            return book(timestamp=NOW * 1000, bids=[{"price": "0.40", "size": "100"}])

        with patch.object(paper.time, "time", return_value=NOW):
            report = self.journal.portfolio(fetch)
        self.assertEqual(len(fetch_calls), 2)
        self.assertEqual(report["positions"][0]["liquidation_quote_usd"], str(
            Decimal(bought["shares"]) * Decimal("0.40")
            - (Decimal(bought["shares"]) * Decimal("0.1") * Decimal("0.40") * Decimal("0.60")).quantize(Decimal("0.00001"))
        ))
        self.assertLess(Decimal(report["change_from_start_usd"]), 0)
        self.assertGreaterEqual(self.journal.cash, 0)

    def test_buy_copies_his_side_at_five_shares_when_rules_pass(self):
        result = self.journal.process("buy-1", row(price="0.50"), market(), book(), NOW)
        self.assertEqual(result["status"], "PAPER_BUY")
        self.assertFalse(result["rule_skipped"])
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["slug"], row()["slug"])
        self.assertEqual(Decimal(result["shares"]), Decimal("5"))
        self.assertLessEqual(Decimal(result["simulated_vwap"]), Decimal(result["source_price"]))

    def test_paying_above_his_fill_is_skipped_without_a_debit(self):
        late = self.journal.process(
            "late", row(price="0.50"), market(),
            book(asks=[{"price": "0.51", "size": "100"}], bids=[{"price": "0.49", "size": "100"}]), NOW,
        )
        self.assertEqual(late["status"], "SKIP")
        self.assertEqual(late["reason"], "latency_worse_than_leader_price")
        self.assertTrue(late["rule_skipped"])
        self.assertEqual(late["slug"], row()["slug"])
        self.assertGreater(Decimal(late["cent_difference"]), 0)
        self.assertEqual(self.journal.cash, Decimal("48"))
        better = self.journal.process(
            "better", row(transaction_hash="0xbetter", price="0.52"), market(), book(), NOW,
        )
        self.assertEqual(better["status"], "PAPER_BUY")
        self.assertFalse(better["rule_skipped"])
        self.assertLessEqual(Decimal(better["simulated_vwap"]), Decimal("0.52"))

    def test_exit_realizes_a_gain_when_bid_is_above_paper_cost(self):
        bought = self.journal.process("buy-1", row(price="0.50"), market(), book(), NOW)
        self.assertEqual(bought["status"], "PAPER_BUY")
        held = self.journal.realize_if_bid_above_cost(
            market(), book(timestamp=NOW * 1000, bids=[{"price": "0.50", "size": "100"}]), NOW,
        )
        self.assertIsNone(held)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], Decimal(bought["shares"]))
        exited = self.journal.realize_if_bid_above_cost(
            market(), book(timestamp=NOW * 1000, bids=[{"price": "0.60", "size": "100"}]), NOW,
        )
        self.assertEqual(exited["status"], "PAPER_SELL")
        self.assertEqual(exited["reason"], "bid_above_paper_cost")
        self.assertGreater(Decimal(exited["realized_pnl_usd"]), 0)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertGreater(self.journal.cash, Decimal("48") - Decimal(bought["gross"]) - Decimal(bought["fee"]))
        report = self.journal.portfolio(lambda url, params=None: market() if "/markets/slug/" in url else book(timestamp=NOW * 1000))
        self.assertGreater(Decimal(report["realized_pnl_usd"]), 0)

    def test_price_band_buys_the_edges_and_skips_outside(self):
        policy = config(copy_price_min="0.40", copy_price_max="0.60", max_open_cost_usd="12")
        journal = paper.PaperJournal(Path(self.tmp.name) / "band.sqlite", policy)
        self.addCleanup(journal.db.close)
        below = journal.process(
            "below", row(transaction_hash="0xbelow", price="0.39"), market(),
            book(asks=[{"price": "0.39", "size": "100"}], bids=[{"price": "0.38", "size": "100"}]), NOW,
        )
        self.assertEqual(below["status"], "SKIP")
        self.assertEqual(below["reason"], "outside_copy_price_band")
        self.assertEqual(below["his_price"], "0.39")
        self.assertIn("decision_latency_seconds", below)
        self.assertEqual(journal.cash, Decimal("48"))
        low = journal.process(
            "low", row(transaction_hash="0xlow", price="0.40"), market(),
            book(asks=[{"price": "0.40", "size": "100"}], bids=[{"price": "0.39", "size": "100"}]), NOW,
        )
        self.assertEqual(low["status"], "PAPER_BUY")
        self.assertEqual(Decimal(low["shares"]), Decimal("5"))
        self.assertLessEqual(Decimal(low["simulated_vwap"]), Decimal("0.40"))
        above = journal.process(
            "above", row(transaction_hash="0xabove", price="0.61"), market(),
            book(asks=[{"price": "0.61", "size": "100"}], bids=[{"price": "0.60", "size": "100"}]), NOW,
        )
        self.assertEqual(above["reason"], "outside_copy_price_band")
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal("5"))
        high = journal.process(
            "high", row(transaction_hash="0xhigh", price="0.60", token_id="44444"),
            market(clobTokenIds=["44444", TOKEN_DOWN]),
            book(asset_id="44444", asks=[{"price": "0.60", "size": "100"}], bids=[{"price": "0.59", "size": "100"}]), NOW,
        )
        self.assertEqual(high["status"], "PAPER_BUY")
        self.assertEqual(Decimal(high["our_price"]), Decimal("0.60"))
        self.assertEqual(Decimal(high["cent_difference"]), Decimal("0"))

    def test_exit_window_sells_only_inside_the_same_minute(self):
        policy = config(exit_window_seconds=60)
        journal = paper.PaperJournal(Path(self.tmp.name) / "window.sqlite", policy)
        self.addCleanup(journal.db.close)
        bought = journal.process("buy-1", row(timestamp=NOW - 10, price="0.50"), market(), book(), NOW)
        self.assertEqual(bought["status"], "PAPER_BUY")
        self.assertEqual(journal.holdings()[TOKEN_UP]["entry_source_timestamp"], NOW - 10)
        late_book = book(timestamp=(NOW + 80) * 1000, bids=[{"price": "0.90", "size": "100"}])
        self.assertIsNone(journal.realize_if_bid_above_cost(market(), late_book, NOW + 80))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal(bought["shares"]))
        inside = journal.realize_if_bid_above_cost(
            market(), book(timestamp=NOW * 1000, bids=[{"price": "0.70", "size": "100"}]), NOW,
        )
        self.assertEqual(inside["status"], "PAPER_SELL")
        self.assertGreater(Decimal(inside["realized_pnl_usd"]), 0)
        self.assertEqual(inside["decision_latency_seconds"], 10)
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)

    def test_fresh_trades_decides_the_newest_fill_first(self):
        older = row(transaction_hash="0xold", timestamp=NOW - 5, price="0.41")
        newer = row(transaction_hash="0xnew", timestamp=NOW - 1, price="0.59")
        found = paper.fresh_trades([older, newer], lambda key: False, config(), NOW - 30)
        self.assertEqual([item[1]["transaction_hash"] for item in found], ["0xnew", "0xold"])
        self.assertEqual(paper.fresh_trades([older, newer], lambda key: key == found[0][0], config(), NOW - 30)[0][1]["transaction_hash"], "0xold")
        kept = paper.fresh_trades([older, newer], lambda key: False, config(), NOW - 30, preserve_order=True)
        self.assertEqual([item[1]["transaction_hash"] for item in kept], ["0xold", "0xnew"])

    def test_stream_trade_row_keeps_only_his_fill(self):
        payload = {
            "proxyWallet": WALLET, "transactionHash": "0xhash", "conditionId": CONDITION,
            "asset": TOKEN_UP, "timestamp": NOW - 1, "side": "BUY", "size": 5, "price": 0.5,
            "slug": f"btc-updown-5m-{START}", "outcome": "Up",
        }
        mapped = paper.stream_trade_row(payload, WALLET)
        self.assertEqual(mapped["side"], "BUY")
        self.assertEqual(mapped["token_id"], TOKEN_UP)
        self.assertEqual(mapped["slug"], payload["slug"])
        self.assertEqual(mapped["timestamp"], NOW - 1)
        self.assertIsNone(paper.stream_trade_row(dict(payload, proxyWallet="0x" + "b" * 40), WALLET))
        self.assertEqual(paper.stream_trade_row(dict(payload, timestamp=(NOW - 1) * 1000), WALLET)["timestamp"], NOW - 1)


if __name__ == "__main__":
    unittest.main()
