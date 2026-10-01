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
        "size": "8",
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
        self.assertEqual(shares, Decimal("8"))
        self.assertEqual(Decimal(result["his_shares"]), Decimal("8"))
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
            "sell-1", row(transaction_hash="0xsell", side="SELL", size="2", price="0.60"),
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
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["leader_shares"],Decimal("8"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"],0)

    def test_leader_size_below_five_is_skipped_and_not_rounded_up(self):
        small = self.journal.process(
            "small", row(size="4"), market(), book(min_order_size="5"), NOW,
        )
        smaller = self.journal.process(
            "smaller", row(transaction_hash="0xsmaller", size="3"), market(),
            book(min_order_size="1"), NOW,
        )
        self.assertEqual(small["status"], "SKIP")
        self.assertEqual(small["reason"], "leader_size_below_market_minimum")
        self.assertEqual(smaller["status"], "SKIP")
        self.assertEqual(smaller["reason"], "leader_size_below_market_minimum")
        self.assertEqual(Decimal(small["his_shares"]), Decimal("4"))
        self.assertEqual(Decimal(smaller["his_shares"]), Decimal("3"))
        self.assertTrue(small["rule_skipped"])
        self.assertEqual(self.journal.cash, Decimal("48"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["cost"], 0)

    def test_full_size_above_cash_is_skipped_and_not_scaled(self):
        too_big = self.journal.process("big", row(size="100"), market(), book(), NOW)
        self.assertEqual(too_big["status"], "SKIP")
        self.assertEqual(too_big["reason"], "leader_size_costs_more_than_cash")
        self.assertEqual(self.journal.cash, Decimal("48"))
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)
        first = self.journal.process("buy-1", row(transaction_hash="0xfit", size="80"), market(), book(), NOW)
        self.assertEqual(first["status"], "PAPER_BUY")
        self.assertEqual(Decimal(first["shares"]), Decimal("80"))
        cash_after = self.journal.cash
        other_row = row(transaction_hash="0xother", size="20", condition_id="0xothercondition", token_id="33333")
        other_market = market(conditionId="0xothercondition", clobTokenIds=["33333", TOKEN_DOWN])
        other_book = book(asset_id="33333", market="0xothercondition")
        second = self.journal.process("buy-2", other_row, other_market, other_book, NOW)
        self.assertEqual(second["status"], "SKIP")
        self.assertEqual(second["reason"], "leader_size_costs_more_than_cash")
        self.assertEqual(self.journal.cash, cash_after)
        self.assertEqual(self.journal.holdings()["33333"]["shares"], 0)
        self.assertEqual(Decimal(second["his_shares"]), Decimal("20"))

    def test_buy_copies_his_exact_share_count(self):
        result = self.journal.process("exact", row(size="12", price="0.50"), market(), book(), NOW)
        self.assertEqual(result["status"], "PAPER_BUY")
        self.assertEqual(Decimal(result["shares"]), Decimal("12"))
        self.assertEqual(Decimal(result["his_shares"]), Decimal("12"))
        self.assertLessEqual(Decimal(result["simulated_vwap"]), Decimal("0.50"))
        thin = self.journal.process(
            "thin", row(transaction_hash="0xthin", size="10"), market(),
            book(asks=[{"price": "0.50", "size": "6"}, {"price": "0.51", "size": "100"}]), NOW,
        )
        self.assertEqual(thin["status"], "SKIP")
        self.assertEqual(thin["reason"], "leader_size_not_at_or_better_than_leader_fill")
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], Decimal("12"))

    def test_optional_minimum_size_allowance_buys_only_required_quantity(self):
        policy = config(target_buy_usd="2.40",max_buy_usd="4.80",max_outcome_cost_usd="4.80")
        journal = paper.PaperJournal(Path(self.tmp.name)/"minimum.sqlite",policy)
        self.addCleanup(journal.db.close)
        result = journal.process("minimum",row(size="12", price="0.70"),market(),
                                 book(min_order_size="5",asks=[{"price":"0.70","size":"100"}],
                                      bids=[{"price":"0.69","size":"100"}]),NOW)
        self.assertEqual(result["status"],"PAPER_BUY")
        self.assertEqual(Decimal(result["shares"]),Decimal("12"))
        self.assertEqual(Decimal(result["his_shares"]),Decimal("12"))

    def test_minimum_allowance_cannot_exceed_hard_cap(self):
        policy = config(target_buy_usd="2.40",max_buy_usd="4.80",max_outcome_cost_usd="4.80")
        journal = paper.PaperJournal(Path(self.tmp.name)/"unaffordable.sqlite",policy)
        self.addCleanup(journal.db.close)
        result = journal.process("expensive",row(size="100", price="0.97"),market(),
                                 book(min_order_size="5",asks=[{"price":"0.97","size":"100"}],
                                      bids=[{"price":"0.96","size":"100"}]),NOW)
        self.assertEqual(result["status"],"SKIP")
        self.assertEqual(result["reason"],"leader_size_costs_more_than_cash")
        self.assertEqual(journal.cash,Decimal("48"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)
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

    def test_buy_copies_his_side_and_share_count_when_rules_pass(self):
        result = self.journal.process("buy-1", row(size="5", price="0.50"), market(), book(), NOW)
        self.assertEqual(result["status"], "PAPER_BUY")
        self.assertFalse(result["rule_skipped"])
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["slug"], row()["slug"])
        self.assertEqual(Decimal(result["shares"]), Decimal("5"))
        self.assertEqual(Decimal(result["his_shares"]), Decimal("5"))
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
        self.assertEqual(exited["source_to_decision_seconds"], 1)
        self.assertGreater(Decimal(exited["realized_pnl_usd"]), 0)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertGreater(self.journal.cash, Decimal("48") - Decimal(bought["gross"]) - Decimal(bought["fee"]))
        report = self.journal.portfolio(lambda url, params=None: market() if "/markets/slug/" in url else book(timestamp=NOW * 1000))
        self.assertGreater(Decimal(report["realized_pnl_usd"]), 0)


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.journal = paper.PaperJournal(self.path, config())

    def test_losing_resolution_pays_zero_and_realizes_the_cost(self):
        bought = self.journal.process("buy-1", row(size="5", price="0.50"), market(), book(), NOW)
        cost = Decimal(bought["gross"]) + Decimal(bought["fee"])
        cash_after_buy = self.journal.cash
        settled = self.journal.settle_resolved(TOKEN_UP, "0", "Down", "gamma")
        self.assertEqual(settled["status"], "SETTLEMENT")
        self.assertEqual(settled["resolved_outcome"], "Down")
        self.assertEqual(Decimal(settled["payout_per_share"]), 0)
        self.assertEqual(Decimal(settled["payout_usd"]), 0)
        self.assertEqual(self.journal.cash, cash_after_buy)
        self.assertEqual(Decimal(settled["realized_pnl_usd"]), -cost)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["cost"], 0)
        report = self.journal.portfolio(lambda url, params=None: None)
        self.assertEqual(Decimal(report["realized_pnl_usd"]), -cost)
        self.assertEqual(Decimal(report["open_cost_usd"]), 0)
        self.assertEqual(report["unresolved_positions"], 0)
        self.assertEqual(Decimal(report["equity_at_liquidation_quote_usd"]), self.journal.cash)

    def test_winning_resolution_pays_one_per_share(self):
        bought = self.journal.process("buy-1", row(size="5", price="0.50"), market(), book(), NOW)
        shares = Decimal(bought["shares"])
        cost = Decimal(bought["gross"]) + Decimal(bought["fee"])
        cash_after_buy = self.journal.cash
        settled = self.journal.settle_resolved(TOKEN_UP, "1", "Up", "gamma")
        self.assertEqual(self.journal.cash, cash_after_buy + shares)
        self.assertEqual(Decimal(settled["payout_usd"]), shares)
        self.assertEqual(Decimal(settled["realized_pnl_usd"]), shares - cost)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], 0)

    def test_settlement_rejects_a_payout_between_zero_and_one(self):
        bought = self.journal.process("buy-1", row(size="5"), market(), book(), NOW)
        cash_after_buy = self.journal.cash
        with self.assertRaises(ValueError):
            self.journal.settle_resolved(TOKEN_UP, "0.5", "Up", "gamma")
        self.assertEqual(self.journal.cash, cash_after_buy)
        self.assertEqual(self.journal.holdings()[TOKEN_UP]["shares"], Decimal(bought["shares"]))


class EntryMinuteExitTests(unittest.TestCase):
    def test_exit_qualifies_only_within_sixty_seconds_of_his_fill(self):
        self.assertTrue(paper.exit_in_entry_minute(1000, 1000))
        self.assertTrue(paper.exit_in_entry_minute(1000, 1060))
        self.assertFalse(paper.exit_in_entry_minute(1000, 1060.1))
        self.assertFalse(paper.exit_in_entry_minute(None, 1000))


if __name__ == "__main__":
    unittest.main()
