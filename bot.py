"""Public Polymarket trade watcher and paper BUY planner. Never submits orders."""
import argparse
import hashlib
import http.client
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite number")
    return result


def validate(config):
    config = dict(config)
    if not isinstance(config.get("leader_wallet"), str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{40}", config["leader_wallet"]
    ):
        raise ValueError("leader_wallet must be the trader's public trading wallet")
    if not isinstance(config.get("timeframes_minutes"), list) or not config["timeframes_minutes"] or any(
        type(x) is not int or x not in (5, 15, 60) for x in config["timeframes_minutes"]
    ):
        raise ValueError("This prototype supports 5-minute, 15-minute and hourly BTC markets")
    for key in ("copy_ratio", "max_trade_usd", "max_daily_proposed_usd",
                "max_signal_age_seconds", "min_seconds_to_expiry", "poll_seconds"):
        if decimal(config[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    if not 0 <= decimal(config["max_price_drift"]) < 1:
        raise ValueError("max_price_drift must be between 0 and 1 (exclusive)")
    if decimal(config["poll_seconds"]) < 1:
        raise ValueError("poll_seconds must be at least 1")
    age = decimal(config["max_signal_age_seconds"])
    if age != age.to_integral_value():
        raise ValueError("max_signal_age_seconds must be a whole number")
    config["max_signal_age_seconds"] = int(age)
    for key in ("min_seconds_to_expiry", "poll_seconds"):
        config[key] = float(decimal(config[key]))
        if not 0 < config[key] < float("inf"):
            raise ValueError(f"{key} must be a finite positive number")
    return config


class RateLimited(Exception):
    """A public host answered 429, or a later call is still inside that pause.

    fresh is true only for the response that carried the 429. Calls made
    while the pause lasts do not open a connection.
    """

    def __init__(self, host, retry_after, fresh=False):
        self.host = host
        self.retry_after = retry_after
        self.fresh = fresh
        super().__init__(f"rate_limited:{host}")


class _Flight:
    def __init__(self):
        self.ready = threading.Event()
        self.value = None
        self.error = None


class PublicClient:
    """One shared keep-alive pool for public GETs.

    Gamma market payloads are reused for a short time. CLOB books are reused
    even more briefly so several pollers do not each hit /book. A 429 pauses
    that host. Nothing here submits an order.
    """

    market_ttl = 15.0
    book_ttl = 1.0
    max_connections_per_host = 2

    def __init__(self, transport=None, clock=None):
        self._transport = transport
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pool = threading.Condition()
        self._idle = {}
        self._live = {}
        self._until = {}
        self._penalty = {}
        self._cache = {}
        self._inflight = {}

    def get_json(self, base, params=None):
        url = base + ("?" + urlencode(params) if params else "")
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("invalid_url")
        remaining = self._backoff_remaining(host)
        if remaining is not None:
            raise RateLimited(host, remaining, fresh=False)
        with self._lock:
            flight = self._inflight.get(url)
            if flight is None:
                flight = _Flight()
                self._inflight[url] = flight
                owner = True
            else:
                owner = False
        if not owner:
            if not flight.ready.wait(12):
                raise TimeoutError("public_get_timed_out")
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            cached = self._read_cache(url)
            if cached is not None:
                flight.value = cached
                return cached
            remaining = self._backoff_remaining(host)
            if remaining is not None:
                raise RateLimited(host, remaining, fresh=False)
            parts = urlsplit(url)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            status, headers, body = self._exchange(host, path)
            if status == 429:
                delay = self._note_429(host, headers.get("retry-after"))
                raise RateLimited(host, delay, fresh=True)
            if status != 200:
                raise ValueError(f"http_{status}")
            self._note_success(host)
            payload = json.loads(body)
            self._write_cache(url, payload)
            flight.value = payload
            return payload
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            flight.ready.set()
            with self._lock:
                if self._inflight.get(url) is flight:
                    del self._inflight[url]

    def _ttl(self, url):
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host == "gamma-api.polymarket.com" and "/markets/" in parts.path:
            return self.market_ttl
        if host == "clob.polymarket.com" and parts.path == "/book":
            return self.book_ttl
        return None

    def _read_cache(self, url):
        ttl = self._ttl(url)
        if ttl is None:
            return None
        with self._lock:
            item = self._cache.get(url)
            if item is None:
                return None
            stored, payload = item
            if self._clock() - stored >= ttl:
                return None
            return payload

    def _write_cache(self, url, payload):
        if self._ttl(url) is None:
            return
        with self._lock:
            self._cache[url] = (self._clock(), payload)

    def _backoff_remaining(self, host):
        with self._lock:
            until = self._until.get(host)
            if until is None:
                return None
            remaining = until - self._clock()
            if remaining <= 0:
                return None
            return remaining

    def _note_429(self, host, retry_after):
        parsed = _retry_after_seconds(retry_after)
        with self._lock:
            if parsed is None:
                delay = self._penalty.get(host, 1.0)
            else:
                delay = parsed
            delay = max(float(delay), 1.0)
            self._penalty[host] = min(delay * 2, 30.0)
            self._until[host] = self._clock() + delay
            return delay

    def _note_success(self, host):
        with self._lock:
            self._penalty[host] = 1.0

    def _exchange(self, host, path):
        if self._transport is not None:
            return self._transport(host, path)
        return self._pooled(host, path)

    def _pooled(self, host, path):
        conn, created = self._checkout(host)
        try:
            if created:
                conn = http.client.HTTPSConnection(host, timeout=10)
            conn.request("GET", path, headers={
                "User-Agent": "btc-copy-paper-prototype/0.1",
                "Accept": "application/json",
                "Connection": "keep-alive",
            })
            response = conn.getresponse()
            body = response.read()
            status = response.status
            headers = {key.lower(): value for key, value in response.getheaders()}
        except Exception:
            self._drop(host, conn)
            raise
        if status == 429:
            self._drop(host, conn)
            return status, headers, body
        self._give_back(host, conn)
        return status, headers, body

    def _checkout(self, host):
        with self._pool:
            while True:
                idle = self._idle.get(host)
                if idle:
                    return idle.pop(), False
                live = self._live.get(host, 0)
                if live < self.max_connections_per_host:
                    self._live[host] = live + 1
                    return None, True
                if not self._pool.wait(timeout=10):
                    raise TimeoutError("connection_pool_exhausted")

    def _give_back(self, host, conn):
        with self._pool:
            self._idle.setdefault(host, []).append(conn)
            self._pool.notify()

    def _drop(self, host, conn):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with self._pool:
            self._live[host] = max(0, self._live.get(host, 1) - 1)
            self._pool.notify()


def _retry_after_seconds(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


client = PublicClient()


def get_json(base, params=None):
    return client.get_json(base, params)


def activity(wallet, start, end, fetch=get_json):
    """Fetch a bounded snapshot; a failed page discards the entire snapshot."""
    params = dict(user=wallet, type="TRADE", start=start, end=end,
                  limit=100, sort_direction="ASC")
    rows, cursors = [], set()
    for _ in range(100):
        page = fetch("https://data-api.polymarket.com/v2/activity", params)
        if not isinstance(page.get("data"), list):
            raise ValueError("Unexpected activity API response")
        rows.extend(page["data"])
        pagination = page["pagination"]
        if not pagination["has_more"]:
            return rows
        cursor = pagination.get("next_cursor")
        if not cursor or cursor in cursors:
            raise ValueError("Missing or repeating activity cursor")
        cursors.add(cursor)
        params["cursor"] = cursor
    raise ValueError("Activity page limit exceeded; snapshot not processed")


def array(value):
    return json.loads(value) if isinstance(value, str) else value


def market_timeframe(slug):
    if not isinstance(slug, str):
        return None
    match = re.fullmatch(r"btc-updown-(5|15)m-(\d+)", slug)
    if match:
        return int(match[1])
    if re.fullmatch(
        r"bitcoin-up-or-down-(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)-([1-9]|[12]\d|3[01])-\d{4}-"
        r"(1[0-2]|[1-9])(am|pm)-et", slug
    ):
        return 60
    return None


def source_skip(row, config, now):
    """Check source identity and age before fetching market data."""
    if row.get("type") != "TRADE" or row.get("is_combo"):
        return "unsupported_activity"
    wallet = row.get("proxy_wallet")
    if not isinstance(wallet, str) or wallet.lower() != config["leader_wallet"].lower():
        return "different_wallet"
    timeframe = market_timeframe(row.get("slug"))
    if timeframe not in config["timeframes_minutes"]:
        return "different_market_or_timeframe"
    timestamp = decimal(row["timestamp"])
    if timestamp != timestamp.to_integral_value():
        return "invalid_source_timestamp"
    age = now - int(timestamp)
    if age < 0 or age > config["max_signal_age_seconds"]:
        return "stale_or_future_signal"
    if row.get("side") != "BUY":
        return "sell_observed_inventory_tracking_required" if row.get("side") == "SELL" else "unsupported_side"
    if not isinstance(row.get("condition_id"), str) or not row["condition_id"]:
        return "market_identity_mismatch"
    if not row.get("token_id"):
        return "outcome_token_mismatch"
    return None


def plan(row, market, book, config, now, proposed_today=Decimal(0)):
    """Validate a BUY against the current ask depth; returns a proposal or skip."""
    skip = lambda reason: {"status": "SKIP", "reason": reason}
    reason = source_skip(row, config, now)
    if reason:
        return skip(reason)
    match = re.fullmatch(r"btc-updown-(5|15)m-(\d+)", row["slug"])
    age = now - int(decimal(row["timestamp"]))
    if (market.get("slug") != row["slug"]
            or market.get("conditionId") != row.get("condition_id")):
        return skip("market_identity_mismatch")
    if (market.get("active") is not True or market.get("closed") is not False
            or market.get("acceptingOrders") is not True
            or market.get("archived") is True):
        return skip("market_not_tradable")
    end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    if end.tzinfo is None:
        return skip("market_expiry_missing_timezone")
    if match:
        start = int(match[2])
        expiry = min(end.timestamp(), start + int(match[1]) * 60)
    else:
        # Hourly slugs use ET. Gamma eventStartTime is the interval's actual
        # timezone-aware start; startDate is only the market creation date.
        if not market.get("eventStartTime"):
            return skip("hourly_market_start_unavailable")
        start_date = datetime.fromisoformat(market["eventStartTime"].replace("Z", "+00:00"))
        if start_date.tzinfo is None:
            return skip("hourly_market_start_missing_timezone")
        start, expiry = start_date.timestamp(), end.timestamp()
        if expiry - start != 3600:
            return skip("unsupported_hourly_market_interval")
    if now < start or expiry - now < config["min_seconds_to_expiry"]:
        return skip("market_not_started_or_near_expiry")
    tokens, outcomes = array(market["clobTokenIds"]), array(market["outcomes"])
    token = str(row["token_id"])
    if (len(tokens) != 2 or len(set(tokens)) != 2
            or len(outcomes) != 2 or set(outcomes) != {"Up", "Down"}):
        return skip("unsupported_outcomes")
    if token not in tokens or outcomes[tokens.index(token)] != row.get("outcome"):
        return skip("outcome_token_mismatch")
    if book.get("asset_id") != token or book.get("market") != row["condition_id"]:
        return skip("book_identity_mismatch")
    if book.get("timestamp") is None:
        return skip("stale_or_future_book")
    book_age_ms = decimal(now) * 1000 - decimal(book["timestamp"])
    if book_age_ms < -1000 or book_age_ms > decimal(config["max_signal_age_seconds"]) * 1000:
        return skip("stale_or_future_book")
    price = decimal(row["price"])
    shares = decimal(row["size"])
    if not 0 < price < 1 or shares <= 0:
        return skip("invalid_source_trade")
    cap = min(Decimal("0.99"), price + decimal(config["max_price_drift"]))
    tick = decimal(book["tick_size"])
    if tick <= 0:
        return skip("invalid_tick")
    limit = (cap / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    # Size by cash, reserving the full limit-price cost rather than best-ask cost.
    budget = min(shares * price * decimal(config["copy_ratio"]),
                 decimal(config["max_trade_usd"]),
                 decimal(config["max_daily_proposed_usd"]) - proposed_today)
    if budget <= 0 or limit <= 0:
        return skip("daily_proposal_budget_exhausted")
    quantity = (budget / limit).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    minimum = decimal(book["min_order_size"])
    if minimum <= 0:
        return skip("invalid_market_minimum")
    if quantity <= 0 or quantity < minimum:
        return skip("below_market_minimum")
    depth = Decimal(0)
    for level in book["asks"]:
        ask, available = decimal(level["price"]), decimal(level["size"])
        if not 0 < ask < 1 or available < 0:
            return skip("invalid_book_level")
        if ask <= limit:
            depth += available
    if depth < quantity:
        return skip("insufficient_depth_within_price_limit")
    return {"status": "PROPOSED_BUY", "token_id": token, "outcome": row["outcome"],
            "slug": row["slug"], "shares": str(quantity), "limit_price": str(limit),
            "max_cost_usd_excluding_fees": str(quantity * limit),
            "source_age_seconds": age, "seconds_to_expiry": int(expiry - now),
            "executed": False}


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS decisions "
                        "(id TEXT PRIMARY KEY, day TEXT, budget TEXT, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata "
                        "(key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def bind(self, config):
        # Reusing decisions under a different strategy could silently hide signals.
        encoded = json.dumps(config, sort_keys=True)
        stored = self.db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        if stored and stored[0] != encoded:
            raise ValueError("Configuration changed: select a fresh --db file")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('config', ?)", (encoded,))

    def contains(self, key):
        return self.db.execute("SELECT 1 FROM decisions WHERE id=?", (key,)).fetchone() is not None

    def spent(self, day):
        return sum((decimal(r[0]) for r in self.db.execute(
            "SELECT budget FROM decisions WHERE day=?", (day,))), Decimal(0))

    def save(self, key, day, decision):
        budget = decision.get("max_cost_usd_excluding_fees", "0")
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)",
                            (key, day, budget, json.dumps(decision)))


def row_keys(rows):
    # Data API does not expose a log index. Multiplicity preserves identical rows
    # in one snapshot; each poll must cover the entire bounded time window.
    counts = {}
    fields = ("proxy_wallet", "transaction_hash", "condition_id", "token_id",
              "timestamp", "side", "size", "price", "usdc_size", "type")
    for row in rows:
        identity = json.dumps([row.get(k) for k in fields], separators=(",", ":"))
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        yield f"{fingerprint}:{counts[fingerprint]}", row


def poll(config, journal, fetch=get_json):
    now = int(time.time())
    rows = activity(config["leader_wallet"], now - config["max_signal_age_seconds"], now, fetch)
    for key, row in row_keys(rows):
        if journal.contains(key):
            continue
        timeframe = market_timeframe(row.get("slug"))
        if timeframe not in config["timeframes_minutes"]:
            continue
        reason = source_skip(row, config, int(time.time()))
        if reason:
            market = book = None
        else:
            # Fetch failures propagate: retry this signal next poll; never mark it seen.
            market = fetch("https://gamma-api.polymarket.com/markets/slug/" + quote(row["slug"], safe=""))
            book = fetch("https://clob.polymarket.com/book", {"token_id": row["token_id"]})
        # Capture one UTC day for both the budget check and its persisted charge.
        # Network requests stay outside the transaction.
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            if journal.contains(key):
                continue
            current = int(time.time())
            day = datetime.fromtimestamp(current, timezone.utc).date().isoformat()
            decision = ({"status": "SKIP", "reason": reason} if reason else
                        plan(row, market, book, config, current, journal.spent(day)))
            decision.update(source_transaction=row.get("transaction_hash"), source_side=row.get("side"))
            journal.save(key, day, decision)
        print(json.dumps(decision), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--db", default="paper.sqlite3")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = validate(json.loads(Path(args.config).read_text()))
    journal = Journal(args.db)
    journal.bind(config)
    failures = 0
    while True:
        try:
            poll(config, journal)
            failures = 0
        except Exception as exc:
            if args.once:
                raise
            failures += 1
            print(json.dumps({"status": "ERROR", "message": str(exc)}), flush=True)
        if args.once:
            break
        time.sleep(min(60, config["poll_seconds"] * (2 ** min(failures, 5))))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
