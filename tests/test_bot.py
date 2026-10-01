import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bot


WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
NOW = 1_800_000_000
START = NOW - 120
SLUG = f"btc-updown-5m-{START}"
CONDITION = "0xcondition"
TOKEN_UP = "12345678901234567890"
TOKEN_DOWN = "98765432109876543210"


def config():
    return {
        "leader_wallet": WALLET,
        "timeframes_minutes": [5, 15],
        "copy_ratio": "0.5",
        "max_trade_usd": "20",
        "max_daily_proposed_usd": "50",
        "max_signal_age_seconds": 300,
        "min_seconds_to_expiry": 30,
        "max_price_drift": "0.02",
        "poll_seconds": 5,
    }


def trade(**overrides):
    row = {
        "proxy_wallet": WALLET,
        "transaction_hash": "0xtransaction",
        "condition_id": CONDITION,
        "token_id": TOKEN_UP,
        "timestamp": NOW - 10,
        "side": "BUY",
        "size": "100",
        "price": "0.50",
        "usdc_size": "50",
        "type": "TRADE",
        "slug": SLUG,
        "outcome": "Up",
        "is_combo": False,
    }
    row.update(overrides)
    return row


def market(**overrides):
    result = {
        "slug": SLUG,
        "conditionId": CONDITION,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "archived": False,
        "endDate": datetime.fromtimestamp(START + 300, timezone.utc).isoformat().replace("+00:00", "Z"),
        "clobTokenIds": [TOKEN_UP, TOKEN_DOWN],
        "outcomes": ["Up", "Down"],
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
        "asks": [{"price": "0.51", "size": "100"}, {"price": "0.52", "size": "100"}],
    }
    result.update(overrides)
    return result


def activity_page(rows, has_more=False, cursor=None):
    return {"data": rows, "pagination": {"has_more": has_more, "next_cursor": cursor}}


class PlannerTests(unittest.TestCase):
    def plan(self, row=None, mkt=None, bk=None, **kwargs):
        return bot.plan(row or trade(), mkt or market(), bk or book(), config(), NOW, **kwargs)

    def test_valid_buy_proposes_tick_rounded_order(self):
        decision = self.plan()
        self.assertEqual(decision["status"], "PROPOSED_BUY")
        self.assertEqual(decision["limit_price"], "0.52")
        self.assertEqual(decision["shares"], "38.46")
        self.assertFalse(decision["executed"])

    def test_stale_and_future_signals_are_skipped(self):
        self.assertEqual(self.plan(row=trade(timestamp=NOW - 301))["reason"], "stale_or_future_signal")
        self.assertEqual(self.plan(row=trade(timestamp=NOW + 1))["reason"], "stale_or_future_signal")

    def test_near_expiry_signal_is_skipped(self):
        almost_expired = market(endDate=datetime.fromtimestamp(NOW + 20, timezone.utc).isoformat())
        self.assertEqual(self.plan(mkt=almost_expired)["reason"], "market_not_started_or_near_expiry")

    def test_price_drift_limit_and_depth_are_enforced(self):
        thin = book(asks=[{"price": "0.53", "size": "100"}])
        self.assertEqual(self.plan(bk=thin)["reason"], "insufficient_depth_within_price_limit")

    def test_token_identity_must_match_outcome(self):
        wrong_token = trade(token_id=TOKEN_DOWN, outcome="Up")
        self.assertEqual(self.plan(row=wrong_token)["reason"], "outcome_token_mismatch")

    def test_minimum_market_size_is_enforced(self):
        small = trade(size="1", price="0.50")
        self.assertEqual(self.plan(row=small)["reason"], "below_market_minimum")

    def test_daily_budget_caps_remaining_proposal(self):
        decision = self.plan(proposed_today=Decimal("49"))
        self.assertEqual(decision["status"], "PROPOSED_BUY")
        self.assertLessEqual(Decimal(decision["max_cost_usd_excluding_fees"]), Decimal("1"))
        self.assertEqual(self.plan(proposed_today=Decimal("50"))["reason"], "daily_proposal_budget_exhausted")

    def test_wallet_mismatch_is_skipped(self):
        self.assertEqual(self.plan(row=trade(proxy_wallet=OTHER))["reason"], "different_wallet")

    def test_sell_is_an_observation_skip(self):
        self.assertEqual(self.plan(row=trade(side="SELL"))["reason"], "sell_observed_inventory_tracking_required")

    def test_hourly_market_uses_gamma_utc_interval(self):
        now = int(datetime(2027, 9, 30, 22, 30, tzinfo=timezone.utc).timestamp())
        slug = "bitcoin-up-or-down-september-30-2027-6pm-et"
        hourly_config = config()
        hourly_config["timeframes_minutes"] = [60]
        row = trade(slug=slug, timestamp=now - 10)
        mkt = market(
            slug=slug,
            endDate="2027-09-30T23:00:00Z",
            eventStartTime="2027-09-30T22:00:00Z",
        )
        result = bot.plan(row, mkt, book(timestamp=now * 1000), hourly_config, now)
        self.assertEqual(result["status"], "PROPOSED_BUY")
        self.assertEqual(result["seconds_to_expiry"], 1800)

    def test_hourly_market_rejects_non_hour_gamma_interval(self):
        now = int(datetime(2027, 9, 30, 22, 30, tzinfo=timezone.utc).timestamp())
        slug = "bitcoin-up-or-down-september-30-2027-6pm-et"
        hourly_config = config()
        hourly_config["timeframes_minutes"] = [60]
        row = trade(slug=slug, timestamp=now - 10)
        mkt = market(
            slug=slug,
            endDate="2027-09-30T22:59:00Z",
            eventStartTime="2027-09-30T22:00:00Z",
        )
        result = bot.plan(row, mkt, book(timestamp=now * 1000), hourly_config, now)
        self.assertEqual(result["reason"], "unsupported_hourly_market_interval")


class WatcherTests(unittest.TestCase):
    def test_activity_follows_v2_cursor_pages(self):
        calls = []

        def fetch(url, params):
            calls.append(dict(params))
            if len(calls) == 1:
                return activity_page([trade()], True, "next-page")
            return activity_page([trade(transaction_hash="0xsecond")])

        rows = bot.activity(WALLET, NOW - 300, NOW, fetch)
        self.assertEqual(len(rows), 2)
        self.assertEqual(calls[1]["cursor"], "next-page")

    def test_activity_pagination_failure_discards_snapshot(self):
        def fetch(url, params):
            if "cursor" not in params:
                return activity_page([trade()], True, "cursor")
            raise RuntimeError("page unavailable")

        with self.assertRaisesRegex(RuntimeError, "page unavailable"):
            bot.activity(WALLET, NOW - 300, NOW, fetch)

    def test_sell_is_saved_as_skip_without_market_lookups(self):
        calls = []

        def fetch(url, params=None):
            calls.append(url)
            return activity_page([trade(side="SELL")])

        with tempfile.TemporaryDirectory() as tmp:
            journal = bot.Journal(Path(tmp) / "journal.sqlite")
            journal.bind(config())
            output = io.StringIO()
            with patch.object(bot.time, "time", return_value=NOW), contextlib.redirect_stdout(output):
                bot.poll(config(), journal, fetch)
            self.assertEqual(len(calls), 1)
            self.assertIn("sell_observed_inventory_tracking_required", output.getvalue())
            self.assertEqual(journal.spent(datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()), 0)

    def test_restart_deduplicates_rows_and_preserves_identical_multiplicity(self):
        rows = [trade(), trade()]
        market_calls = []

        def fetch(url, params=None):
            if url.endswith("/v2/activity"):
                return activity_page(rows)
            if "/markets/slug/" in url:
                market_calls.append(url)
                return market()
            return book()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite"
            journal = bot.Journal(path)
            journal.bind(config())
            with patch.object(bot.time, "time", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
                bot.poll(config(), journal, fetch)
            self.assertEqual(len(market_calls), 2)
            journal.db.close()

            restarted = bot.Journal(path)
            restarted.bind(config())
            market_calls.clear()
            with patch.object(bot.time, "time", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
                bot.poll(config(), restarted, fetch)
            self.assertEqual(market_calls, [])
            spent = restarted.spent(datetime.fromtimestamp(NOW, timezone.utc).date().isoformat())
            self.assertGreater(spent, 0)
            self.assertLessEqual(spent, Decimal("50"))
            self.assertEqual(restarted.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 2)


class SharedClientTests(unittest.TestCase):
    def test_429_backs_off_and_does_not_hit_the_host_again(self):
        client = bot.PolymarketClient()
        calls = []

        def exchange(host, path):
            calls.append((host, path))
            return 429, {"Retry-After": "5"}, b""

        client._exchange = exchange
        with self.assertRaises(bot.RateLimited) as caught:
            client.get_json("https://data-api.polymarket.com/v2/activity", {"user": "0xabc"})
        self.assertGreaterEqual(caught.exception.retry_after, 5)
        self.assertGreaterEqual(bot.retry_after_seconds(caught.exception, 0), 5)
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://data-api.polymarket.com/v2/activity", {"user": "0xabc"})
        self.assertEqual(len(calls), 1)
        self.assertGreater(client.seconds_until_allowed("data-api.polymarket.com"), 0)

    def test_gamma_market_reads_share_one_cached_response(self):
        client = bot.PolymarketClient()
        calls = []

        def exchange(host, path):
            calls.append(path)
            return 200, {}, b'{"slug":"btc"}'

        client._exchange = exchange
        first = client.get_json("https://gamma-api.polymarket.com/markets/slug/btc")
        second = client.get_json("https://gamma-api.polymarket.com/markets/slug/btc")
        self.assertEqual(first["slug"], "btc")
        self.assertEqual(second, first)
        self.assertEqual(calls, ["/markets/slug/btc"])

    def test_order_book_is_not_reused_from_the_market_cache(self):
        client = bot.PolymarketClient()
        calls = []

        def exchange(host, path):
            calls.append(path)
            return 200, {}, b'{"asks":[]}'

        client._exchange = exchange
        client.get_json("https://clob.polymarket.com/book", {"token_id": "1"})
        client.get_json("https://clob.polymarket.com/book", {"token_id": "1"})
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
