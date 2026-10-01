import contextlib
import io
import tempfile
import threading
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


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheaders(self):
        return list(self._headers.items())


class ScriptedConnection:
    def __init__(self, host, script, calls):
        self.host = host
        self.script = script
        self.calls = calls

    def request(self, method, path, headers=None):
        self.calls.append((self.host, path))
        self._response = self.script.pop(0)

    def getresponse(self):
        return self._response

    def close(self):
        pass


def scripted_client(script, clock):
    calls = []
    connections = []

    def connect(host):
        connection = ScriptedConnection(host, script, calls)
        connections.append(connection)
        return connection

    client = bot.PublicClient(connect=connect, now=lambda: clock["t"])
    return client, calls, connections


class PublicClientTests(unittest.TestCase):
    def test_429_backs_off_and_does_not_request_again_on_the_next_tick(self):
        clock = {"t": 0.0}
        script = [
            FakeResponse(429, "{}", {"Retry-After": "0"}),
            FakeResponse(200, '{"ok": true}'),
        ]
        client, calls, connections = scripted_client(script, clock)
        with self.assertRaises(bot.RateLimited) as caught:
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        self.assertEqual(caught.exception.host, "clob.polymarket.com")
        self.assertEqual(len(calls), 1)
        clock["t"] = 0.05
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(connections), 1)
        clock["t"] = 1.0
        self.assertEqual(client.get_json("https://clob.polymarket.com/book?token_id=1"), {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(connections), 2)

    def test_missing_retry_after_grows_and_a_success_clears_it(self):
        clock = {"t": 0.0}
        script = [
            FakeResponse(429, "{}"),
            FakeResponse(429, "{}"),
            FakeResponse(200, '{"ok": true}'),
            FakeResponse(200, '{"ok": true}'),
        ]
        client, calls, _ = scripted_client(script, clock)
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        clock["t"] = 1.0
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        clock["t"] = 2.0
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        self.assertEqual(len(calls), 2)
        clock["t"] = 3.0
        self.assertEqual(client.get_json("https://clob.polymarket.com/book?token_id=1"), {"ok": True})
        self.assertEqual(len(calls), 3)
        clock["t"] = 3.05
        self.assertEqual(client.get_json("https://clob.polymarket.com/book?token_id=2"), {"ok": True})

    def test_market_payloads_are_cached_and_books_are_not(self):
        clock = {"t": 0.0}
        script = [
            FakeResponse(200, '{"slug": "one"}'),
            FakeResponse(200, '{"slug": "two"}'),
            FakeResponse(200, '{"bids": []}'),
            FakeResponse(200, '{"bids": [{"price": "0.4", "size": "5"}]}'),
        ]
        client, calls, connections = scripted_client(script, clock)
        market = "https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-1"
        self.assertEqual(client.get_json(market)["slug"], "one")
        self.assertEqual(client.get_json(market)["slug"], "one")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(connections), 1)
        clock["t"] = 15.0
        self.assertEqual(client.get_json(market)["slug"], "two")
        self.assertEqual(len(calls), 2)
        book = "https://clob.polymarket.com/book?token_id=1"
        self.assertEqual(client.get_json(book)["bids"], [])
        self.assertEqual(client.get_json(book)["bids"][0]["price"], "0.4")
        self.assertEqual(len(calls), 4)

    def test_one_host_pool_is_shared_and_a_429_does_not_block_another_host(self):
        clock = {"t": 0.0}
        calls = []

        def connect(host):
            connection = ScriptedConnection(host, [], calls)
            if host.startswith("clob"):
                connection.script.append(FakeResponse(429, "{}", {"Retry-After": "10"}))
            else:
                connection.script.append(FakeResponse(200, '{"cached": false}'))
                connection.script.append(FakeResponse(200, '{"cached": false}'))
            return connection

        client = bot.PublicClient(connect=connect, now=lambda: clock["t"])
        self.assertEqual(client.get_json("https://gamma-api.polymarket.com/markets/slug/abc"), {"cached": False})
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=1")
        self.assertEqual(client.get_json("https://gamma-api.polymarket.com/markets?limit=1"), {"cached": False})
        with self.assertRaises(bot.RateLimited):
            client.get_json("https://clob.polymarket.com/book?token_id=2")
        hosts = [host for host, _ in calls]
        self.assertEqual(hosts.count("clob.polymarket.com"), 1)
        self.assertEqual(hosts.count("gamma-api.polymarket.com"), 2)

    def test_concurrent_pollers_share_one_request(self):
        calls = []
        started = threading.Event()
        release = threading.Event()

        class SlowConnection:
            def request(self, method, path, headers=None):
                calls.append(path)
                started.set()
                release.wait(2)

            def getresponse(self):
                return FakeResponse(200, '{"shared": true}')

            def close(self):
                pass

        client = bot.PublicClient(connect=lambda host: SlowConnection(), now=lambda: 0.0)
        results = []

        def pull():
            results.append(client.get_json("https://clob.polymarket.com/book?token_id=9"))

        first = threading.Thread(target=pull)
        first.start()
        self.assertTrue(started.wait(2))
        second = threading.Thread(target=pull)
        second.start()
        # The second poller joins the in-flight GET instead of sending its own.
        second.join(0.05)
        self.assertTrue(second.is_alive())
        self.assertEqual(len(calls), 1)
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(results, [{"shared": True}, {"shared": True}])


if __name__ == "__main__":
    unittest.main()
