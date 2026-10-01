import json
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


def paper_c_config(**overrides):
    result = config(strategy="paper_c", starting_cash_usd="37.40", max_open_cost_usd="37.39",
                    max_daily_proposed_usd="39")
    result.update(overrides)
    return result


def seed_position(journal, shares, cost):
    payload = json.dumps({"row": row(), "shares": str(shares), "cost": str(cost), "leader_shares": str(shares)})
    journal.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?)", (TOKEN_UP, payload))
    journal.db.commit()


class PaperCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def journal(self, **overrides):
        path = Path(self.tmp.name) / f"paperc-{len(list(Path(self.tmp.name).iterdir()))}.sqlite"
        journal = paper.PaperJournal(path, paper_c_config(**overrides))
        self.addCleanup(journal.db.close)
        return journal

    def test_buy_is_skipped_without_a_book_or_a_new_position(self):
        journal = self.journal()
        result = journal.process("buy-1", row(price="0.42"), None, None, NOW)
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "paper_c_no_new_buys")
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["his_price"], "0.42")
        self.assertIsNone(result["our_price"])
        self.assertIsNone(result["cent_gap"])
        self.assertEqual(result["decision_latency_seconds"], NOW - (NOW - 1))
        self.assertEqual(result["realized_pnl_usd"], "0")
        self.assertEqual(result["unrealized_pnl_usd"], "0")
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)

    def test_buy_with_a_tradable_book_still_does_not_open(self):
        journal = self.journal()
        result = journal.process("buy-book", row(), market(), book(), NOW)
        self.assertEqual(result["reason"], "paper_c_no_new_buys")
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(sum((p["shares"] for p in journal.holdings().values()), Decimal(0)), 0)

    def test_buy_at_his_price_or_better_stays_inside_cash(self):
        journal = self.journal(copy_buys_at_or_better=True)
        copied = journal.process("buy-ok", row(price="0.50", size="10"), market(), book(), NOW)
        self.assertEqual(copied["status"], "PAPER_BUY")
        self.assertEqual(copied["side"], "BUY")
        self.assertEqual(copied["slug"], row()["slug"])
        self.assertEqual(copied["fee_collected_in"], "shares")
        self.assertEqual(Decimal(copied["fee"]), Decimal("0.25000"))
        self.assertEqual(Decimal(copied["share_fee"]), Decimal("0.5"))
        self.assertEqual(Decimal(copied["shares"]), Decimal("9.5"))
        self.assertLessEqual(Decimal(copied["simulated_vwap"]), Decimal(copied["his_price"]))
        cost = journal.holdings()[TOKEN_UP]["cost"]
        self.assertEqual(cost, Decimal("5.00"))
        self.assertEqual(Decimal(copied["ordered_shares"]), Decimal("10"))
        self.assertLessEqual(cost, Decimal("37.40") / 2)
        self.assertEqual(journal.cash + cost, Decimal("37.40"))
        capped = journal.process("buy-capped", row(price="0.50", size="100", transaction_hash="0xbig"), market(), book(), NOW)
        self.assertEqual(capped["status"], "PAPER_BUY")
        self.assertLessEqual(Decimal(capped["ordered_shares"]), Decimal("100"))
        capped_cost = Decimal(capped["gross"])
        self.assertLessEqual(capped_cost, (Decimal("37.40") - cost) / 2)
        self.assertLessEqual(Decimal(capped["simulated_vwap"]), Decimal("0.50"))
        below = journal.process("buy-below", row(price="0.50", size="2", transaction_hash="0xsmall"), market(), book(), NOW)
        self.assertEqual(below["reason"], "below_market_minimum")
        late = journal.process(
            "buy-late", row(transaction_hash="0xlate", price="0.50", size="10"), market(),
            book(asks=[{"price": "0.51", "size": "100"}], bids=[{"price": "0.49", "size": "100"}]), NOW,
        )
        self.assertEqual(late["status"], "SKIP")
        self.assertEqual(late["reason"], "latency_worse_than_leader_price")
        self.assertEqual(late["our_price"], "0.51")
        self.assertEqual(journal.cash + sum((p["cost"] for p in journal.holdings().values()), Decimal(0)), Decimal("37.40"))

    def test_half_cash_cap_never_buys_more_than_his_size_or_a_worse_price(self):
        journal = self.journal(copy_buys_at_or_better=True)
        asks = [{"price": "0.07", "size": "270"}, {"price": "0.09", "size": "100"}]
        copied = journal.process(
            "buy-better",
            row(price="0.08", size="275.48695"),
            market(),
            book(tick_size="0.001", min_order_size="5", asks=asks, bids=[{"price": "0.06", "size": "100"}]),
            NOW,
        )
        self.assertEqual(copied["status"], "PAPER_BUY")
        ordered = Decimal(copied["ordered_shares"])
        self.assertLess(ordered, Decimal("275.48695"))
        self.assertGreaterEqual(ordered, Decimal("5"))
        self.assertLessEqual(Decimal(copied["gross"]), Decimal("37.40") / 2)
        self.assertEqual(Decimal(copied["simulated_vwap"]), Decimal("0.07"))
        self.assertLess(Decimal(copied["simulated_vwap"]), Decimal(copied["his_price"]))
        self.assertEqual(Decimal(copied["shares"]), ordered - Decimal(copied["share_fee"]))
        self.assertGreater(Decimal(copied["fee"]), 0)
        self.assertEqual(journal.cash + journal.holdings()[TOKEN_UP]["cost"], Decimal("37.40"))
        exact = self.journal(copy_buys_at_or_better=True).process(
            "buy-exact",
            row(price="0.08", size="20", transaction_hash="0xexact"),
            market(),
            book(asks=[{"price": "0.07", "size": "100"}], bids=[{"price": "0.06", "size": "10"}]),
            NOW,
        )
        self.assertEqual(exact["status"], "PAPER_BUY")
        self.assertEqual(Decimal(exact["ordered_shares"]), Decimal("20"))
        self.assertLessEqual(Decimal(exact["gross"]), Decimal("37.40") / 2)
        self.assertLessEqual(Decimal(exact["simulated_vwap"]), Decimal("0.08"))
        worse = journal.process(
            "buy-worse",
            row(price="0.05", size="100", transaction_hash="0xworse"),
            market(),
            book(asks=[{"price": "0.06", "size": "100"}], bids=[{"price": "0.04", "size": "10"}]),
            NOW,
        )
        self.assertEqual(worse["status"], "SKIP")
        self.assertEqual(worse["reason"], "latency_worse_than_leader_price")
        self.assertEqual(worse["our_price"], "0.06")
        short = journal.process(
            "buy-short-book",
            row(price="0.08", size="100", transaction_hash="0xshortbook"),
            market(),
            book(asks=[{"price": "0.07", "size": "4"}, {"price": "0.20", "size": "100"}], bids=[{"price": "0.06", "size": "10"}]),
            NOW,
        )
        self.assertEqual(short["status"], "SKIP")
        self.assertEqual(short["reason"], "five_shares_not_at_or_better_than_leader_fill")
        self.assertEqual(journal.cash + sum((p["cost"] for p in journal.holdings().values()), Decimal(0)), Decimal("37.40"))

    def test_five_shares_above_half_the_remaining_cash_are_skipped(self):
        journal = self.journal(copy_buys_at_or_better=True)
        journal.db.execute("UPDATE meta SET value=? WHERE key='cash'", ("8",))
        journal.db.commit()
        result = journal.process(
            "buy-tight", row(price="0.90", size="100"),
            market(), book(asks=[{"price": "0.90", "size": "100"}], bids=[{"price": "0.80", "size": "100"}]), NOW,
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "half_cash_below_five_shares")
        self.assertEqual(journal.cash, Decimal("8"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)

    def test_taker_fee_rounds_to_five_decimals_and_drops_dust(self):
        self.assertEqual(
            paper.taker_fee_usdc(Decimal("10"), Decimal("0.07"), Decimal("0.50")),
            Decimal("0.17500"),
        )
        self.assertEqual(
            paper.taker_fee_usdc(Decimal("1"), Decimal("0.07"), Decimal("0.00002")),
            Decimal("0"),
        )
        self.assertEqual(paper.taker_fee_rate(market(feesEnabled=False)), Decimal("0"))
        self.assertEqual(
            paper.taker_fee_rate(market(category="Geopolitics", feeSchedule={"rate": "0.07", "rebateRate": "0.2"})),
            Decimal("0"),
        )
        self.assertEqual(
            paper.taker_fee_rate(market(feeSchedule=None, feesEnabled=True), f"btc-updown-5m-{START}"),
            Decimal("0.07"),
        )
        self.assertEqual(
            paper.taker_fee_rate(market(feeSchedule={"rate": "0.07", "rebateRate": "0.2", "takerOnly": True},
                                        feeType="crypto_fees_v2", takerBaseFee=1000)),
            Decimal("0.07"),
        )

    def test_fees_off_keep_his_full_share_count(self):
        journal = self.journal(copy_buys_at_or_better=True)
        copied = journal.process(
            "buy-off", row(price="0.50", size="10"), market(feesEnabled=False), book(), NOW,
        )
        self.assertEqual(copied["status"], "PAPER_BUY")
        self.assertEqual(Decimal(copied["fee"]), Decimal("0"))
        self.assertEqual(Decimal(copied["share_fee"]), Decimal("0"))
        self.assertEqual(Decimal(copied["shares"]), Decimal("10"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["cost"], Decimal("5.00"))
        stored = journal.holdings()[TOKEN_UP]
        skipped = journal.process(
            "buy-late-off", row(price="0.40", size="10", transaction_hash="0xother"),
            market(feesEnabled=False),
            book(asks=[{"price": "0.41", "size": "100"}], bids=[{"price": "0.39", "size": "100"}]),
            NOW,
        )
        self.assertEqual(skipped["status"], "SKIP")
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], stored["shares"])
        self.assertEqual(journal.holdings()[TOKEN_UP]["cost"], stored["cost"])

    def test_crypto_schedule_rate_is_taken_in_shares_without_a_maker_rebate(self):
        journal = self.journal(copy_buys_at_or_better=True)
        live = market(feeSchedule={"exponent": 1, "rate": "0.07", "takerOnly": True, "rebateRate": "0.2"},
                      feeType="crypto_fees_v2", takerBaseFee=1000)
        copied = journal.process("buy-crypto", row(price="0.50", size="10"), live, book(), NOW)
        self.assertEqual(Decimal(copied["fee"]), Decimal("0.17500"))
        self.assertEqual(Decimal(copied["share_fee"]), Decimal("0.35"))
        self.assertEqual(Decimal(copied["shares"]), Decimal("9.65"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["cost"], Decimal("5.00"))
        self.assertEqual(journal.cash, Decimal("32.40"))

    def test_same_minute_bid_above_cost_sells_without_his_sell(self):
        journal = self.journal(copy_buys_at_or_better=True, sell_same_minute_if_bid_above_cost=True)
        live = book(asks=[{"price": "0.40", "size": "100"}], bids=[{"price": "0.60", "size": "100"}])
        bought = journal.process("buy-open", row(price="0.50", size="10", timestamp=NOW - 1), market(), live, NOW)
        self.assertEqual(bought["status"], "PAPER_BUY")
        self.assertEqual(Decimal(bought["shares"]), Decimal("9.4"))
        self.assertEqual(Decimal(bought["fee"]), Decimal("0.24000"))
        sold = journal.realize_same_minute_if_bid_above_cost(market(), live, NOW)
        self.assertIsNotNone(sold)
        self.assertEqual(sold["status"], "PAPER_SELL")
        self.assertEqual(sold["reason"], "same_minute_bid_above_paper_cost")
        self.assertEqual(Decimal(sold["fee"]), Decimal("0.22560"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertGreater(Decimal(sold["realized_pnl_usd"]), 0)
        self.assertEqual(sold["unrealized_pnl_usd"], "0")
        self.assertEqual(sold["equity_reached_75"], False)
        self.assertEqual(sold["decision_latency_seconds"], 1)
        self.assertGreater(journal.cash, Decimal("37.40"))
        again = journal.realize_same_minute_if_bid_above_cost(market(), live, NOW)
        self.assertIsNone(again)

    def test_a_later_minute_or_a_thin_bid_does_not_sell(self):
        journal = self.journal(copy_buys_at_or_better=True, sell_same_minute_if_bid_above_cost=True)
        opened = book(asks=[{"price": "0.40", "size": "100"}], bids=[{"price": "0.60", "size": "100"}])
        bought = journal.process("buy-open", row(price="0.50", size="10", timestamp=NOW - 1), market(), opened, NOW)
        self.assertEqual(bought["status"], "PAPER_BUY")
        thin = book(timestamp=NOW * 1000, asks=[{"price": "0.40", "size": "100"}], bids=[{"price": "0.60", "size": "3"}])
        self.assertIsNone(journal.realize_same_minute_if_bid_above_cost(market(), thin, NOW))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal(bought["shares"]))
        later = book(timestamp=(NOW + 60) * 1000, asks=[{"price": "0.40", "size": "100"}], bids=[{"price": "0.60", "size": "100"}])
        self.assertIsNone(journal.realize_same_minute_if_bid_above_cost(market(), later, NOW + 60))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal(bought["shares"]))
        quiet = self.journal(copy_buys_at_or_better=True)
        quiet_buy = quiet.process("buy-quiet", row(price="0.50", size="10", timestamp=NOW - 1), market(), opened, NOW)
        self.assertIsNone(quiet.realize_same_minute_if_bid_above_cost(market(), opened, NOW))
        self.assertEqual(quiet.holdings()[TOKEN_UP]["shares"], Decimal(quiet_buy["shares"]))

    def test_sell_without_a_position_stays_skipped_after_the_buy_rule(self):
        journal = self.journal(copy_buys_at_or_better=True)
        result = journal.process(
            "sell-flat", row(side="SELL", price="0.90", transaction_hash="0xs"),
            None, None, NOW, skip_reason="no_matching_paper_position",
        )
        self.assertEqual(result["reason"], "no_matching_paper_position")
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(sum((p["shares"] for p in journal.holdings().values()), Decimal(0)), 0)

    def test_sell_without_a_position_is_skipped_even_when_the_bid_is_rich(self):
        journal = self.journal()
        result = journal.process(
            "sell-flat", row(side="SELL", price="0.90", transaction_hash="0xs"),
            market(), book(bids=[{"price": "0.90", "size": "100"}]), NOW,
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "no_matching_paper_position")
        self.assertEqual(result["side"], "SELL")
        self.assertIsNone(result["our_price"])
        self.assertEqual(journal.cash, Decimal("37.40"))

    def test_sell_closes_a_matching_position_only_above_paper_cost(self):
        journal = self.journal()
        seed_position(journal, "5", "2")
        blocked = journal.process(
            "sell-cheap", row(side="SELL", price="0.30", transaction_hash="0xcheap"),
            market(), book(bids=[{"price": "0.30", "size": "100"}]), NOW,
        )
        self.assertEqual(blocked["status"], "SKIP")
        self.assertEqual(blocked["reason"], "sell_not_above_paper_cost")
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal("5"))
        copied = journal.process(
            "sell-gain", row(side="SELL", price="0.60", transaction_hash="0xgain"),
            market(), book(bids=[{"price": "0.60", "size": "100"}]), NOW,
        )
        self.assertEqual(copied["status"], "PAPER_SELL")
        self.assertEqual(copied["side"], "SELL")
        self.assertEqual(copied["slug"], row()["slug"])
        self.assertEqual(copied["his_price"], "0.60")
        self.assertEqual(copied["our_price"], "0.60")
        self.assertEqual(Decimal(copied["cent_gap"]), Decimal("0"))
        self.assertGreater(Decimal(copied["realized_pnl_usd"]), 0)
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], 0)
        self.assertGreater(journal.cash, Decimal("37.40"))
        self.assertEqual(copied["unrealized_pnl_usd"], "0")

    def test_tightened_sell_rule_refuses_a_fill_below_his_price(self):
        journal = self.journal(sell_must_match_his_price=True)
        seed_position(journal, "5", "2")
        result = journal.process(
            "sell-short", row(side="SELL", price="0.70", transaction_hash="0xshort"),
            market(), book(bids=[{"price": "0.60", "size": "100"}]), NOW,
        )
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["reason"], "sell_worse_than_his_price")
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal("5"))

    def test_public_resolution_pays_the_published_outcome_price(self):
        journal = self.journal()
        journal.db.execute("UPDATE meta SET value=? WHERE key='cash'", ("30.94321",))
        up = {"row": row(), "shares": "15", "cost": "4.87433", "leader_shares": "15"}
        down_row = row(outcome="Down", token_id=TOKEN_DOWN, transaction_hash="0xdown")
        down = {"row": down_row, "shares": "5", "cost": "3.18246", "leader_shares": "5"}
        journal.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?)", (TOKEN_UP, json.dumps(up)))
        journal.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?)", (TOKEN_DOWN, json.dumps(down)))
        journal.db.commit()
        resolved = {
            "slug": row()["slug"], "closed": True, "umaResolutionStatus": "resolved",
            "outcomes": ["Up", "Down"], "outcomePrices": ["0", "1"],
            "clobTokenIds": [TOKEN_UP, TOKEN_DOWN], "lastTradePrice": 0.01,
            "closedTime": "2026-10-01 01:30:54+00",
        }
        closes = journal.realize_public_resolutions(lambda url, params=None: resolved, NOW)
        by_outcome = {item["outcome"]: item for item in closes}
        self.assertEqual(by_outcome["Up"]["resolution_price"], "0")
        self.assertEqual(by_outcome["Up"]["proceeds_usd"], "0")
        self.assertEqual(Decimal(by_outcome["Up"]["realized_pnl_usd"]), Decimal("-4.87433"))
        self.assertEqual(by_outcome["Down"]["resolution_price"], "1")
        self.assertEqual(Decimal(by_outcome["Down"]["proceeds_usd"]), Decimal("5"))
        self.assertEqual(Decimal(by_outcome["Down"]["realized_pnl_usd"]), Decimal("5") - Decimal("3.18246"))
        self.assertEqual(journal.cash, Decimal("35.94321"))
        self.assertEqual(sum((p["shares"] for p in journal.holdings().values()), Decimal(0)), 0)
        self.assertIsNone(by_outcome["Down"]["decision_latency_seconds"])
        self.assertTrue(any(item.get("equity_reached_75") is False for item in closes))
        self.assertFalse(any(item.get("equity_reached_75") is True for item in closes))
        report = journal.portfolio(lambda url, params=None: resolved)
        self.assertEqual(Decimal(report["realized_pnl_usd"]), Decimal("-3.05679"))
        self.assertEqual(report["unrealized_pnl_at_liquidation_quote_usd"], "0")
        self.assertEqual(journal.realize_public_resolutions(lambda url, params=None: resolved, NOW), [])
        self.assertEqual(journal.cash, Decimal("35.94321"))

    def test_unresolved_market_stays_open_without_a_made_up_price(self):
        journal = self.journal()
        seed_position(journal, "5", "2")
        open_market = {
            "slug": row()["slug"], "closed": False, "umaResolutionStatus": "proposed",
            "outcomes": ["Up", "Down"], "outcomePrices": ["0.4", "0.6"],
            "clobTokenIds": [TOKEN_UP, TOKEN_DOWN], "lastTradePrice": 0.99,
        }
        self.assertEqual(journal.realize_public_resolutions(lambda url, params=None: open_market, NOW), [])
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal("5"))
        self.assertEqual(journal.cash, Decimal("37.40"))
        missing = dict(open_market, closed=True, umaResolutionStatus="resolved", outcomePrices=None)
        unavailable = journal.realize_public_resolutions(lambda url, params=None: missing, NOW)
        self.assertEqual(unavailable[0]["status"], "RESOLUTION_UNAVAILABLE")
        self.assertEqual(journal.holdings()[TOKEN_UP]["shares"], Decimal("5"))
        self.assertEqual(journal.cash, Decimal("37.40"))

    def test_steady_loss_resets_cash_and_changes_one_rule(self):
        self.assertFalse(paper.steadily_losing([Decimal("39"), Decimal("39"), Decimal("39")]))
        self.assertFalse(paper.steadily_losing([Decimal("39"), Decimal("38"), Decimal("38")]))
        self.assertFalse(paper.steadily_losing([Decimal("39"), Decimal("40"), Decimal("41")]))
        self.assertTrue(paper.steadily_losing([Decimal("39"), Decimal("37"), Decimal("35")]))
        journal = self.journal()
        seed_position(journal, "5", "2")
        journal.db.execute("UPDATE meta SET value=? WHERE key='cash'", ("30",))
        journal.db.commit()
        reset = journal.apply_one_rule_reset()
        self.assertEqual(reset["rule_changed"], "sell_must_match_his_price")
        self.assertTrue(reset["open_book_dropped"])
        self.assertEqual(journal.cash, Decimal("37.40"))
        self.assertEqual(journal.holdings(), {})
        self.assertTrue(journal.config["sell_must_match_his_price"])
        with self.assertRaises(ValueError):
            journal.apply_one_rule_reset()


if __name__ == "__main__":
    unittest.main()
