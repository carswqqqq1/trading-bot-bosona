"""Public Polymarket trade watcher and paper BUY planner. Never submits orders."""
import argparse
import copy
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
from urllib.error import HTTPError
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


USER_AGENT = "btc-copy-paper-prototype/0.1"
# Gamma documents identify the market. They are not the executable book.
MARKET_CACHE_SECONDS = 10
POOL_SIZE = 4


class RateLimited(Exception):
    """A host returned 429. retry_after is how long callers must wait before another request."""

    def __init__(self, host, retry_after):
        self.host = host
        self.retry_after = float(retry_after)
        super().__init__(
            f"HTTP Error 429: Too Many Requests; backing off {self.retry_after:.3f}s"
        )


class PublicClient:
    """One shared keep-alive pool for public GETs. Never signs or submits an order.

    A 429 parks that host until the backoff ends. Calls during the pause raise
    RateLimited and do not open a socket. Gamma market documents are cached.
    Order books are not, so a copy still sees the live price.
    """

    def __init__(self, connector=None, clock=None, cache_seconds=MARKET_CACHE_SECONDS,
                 pool_size=POOL_SIZE):
        self._connector = connector
        self._clock = clock or time.monotonic
        self._cache_seconds = cache_seconds
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._idle = {}
        self._slots = {}
        self._backoff_until = {}
        self._backoff_step = {}
        self._cache = {}
        self._inflight = {}

    def get(self, base, params=None):
        url = base + ("?" + urlencode(params) if params else "")
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            raise ValueError("missing_host")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        self._raise_if_blocked(host)
        with self._lock:
            flight = self._inflight.get(url)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._inflight[url] = flight
        if not leader:
            if not flight.event.wait(15):
                raise TimeoutError("public request timed out")
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.value)
        try:
            cached = self._read_cache(url)
            if cached is not None:
                flight.value = cached
                return copy.deepcopy(cached)
            self._raise_if_blocked(host)
            payload = self._fetch(host, path)
            if _cacheable(url):
                self._write_cache(url, payload)
            flight.value = payload
            return copy.deepcopy(payload)
        except Exception as exc:
            flight.error = exc
            raise
        finally:
            flight.event.set()
            with self._lock:
                if self._inflight.get(url) is flight:
                    del self._inflight[url]

    def _fetch(self, host, path):
        sem = self._slot(host)
        sem.acquire()
        conn = None
        broken = False
        try:
            conn = self._acquire(host)
            try:
                conn.request(
                    "GET", path,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Connection": "keep-alive",
                    },
                )
                response = conn.getresponse()
                body = response.read()
            except Exception:
                broken = True
                raise
            if response.status == 429:
                delay = self._penalize(host, response)
                raise RateLimited(host, delay)
            if response.status != 200:
                raise HTTPError(f"https://{host}{path}", response.status, response.reason, None, None)
            self._reset_penalty(host)
            return json.loads(body.decode())
        finally:
            if conn is not None:
                self._release(host, conn, broken)
            sem.release()

    def _acquire(self, host):
        with self._lock:
            idle = self._idle.get(host)
            if idle:
                return idle.pop()
        if self._connector is not None:
            return self._connector(host)
        return http.client.HTTPSConnection(host, timeout=10)

    def _release(self, host, conn, broken):
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            return
        with self._lock:
            idle = self._idle.setdefault(host, [])
            if len(idle) < self._pool_size:
                idle.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def _slot(self, host):
        with self._lock:
            sem = self._slots.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(self._pool_size)
                self._slots[host] = sem
            return sem

    def _raise_if_blocked(self, host):
        remaining = self._remaining(host)
        if remaining > 0:
            raise RateLimited(host, remaining)

    def _remaining(self, host):
        with self._lock:
            until = self._backoff_until.get(host, 0)
        return max(0.0, until - self._clock())

    def _penalize(self, host, response):
        header = response.getheader("Retry-After") if response is not None else None
        parsed = None
        if header is not None:
            try:
                parsed = float(header)
            except (TypeError, ValueError):
                parsed = None
        with self._lock:
            step = self._backoff_step.get(host, 1.0)
            delay = parsed if parsed is not None and parsed > 0 else step
            delay = min(60.0, max(1.0, delay))
            self._backoff_until[host] = self._clock() + delay
            self._backoff_step[host] = min(60.0, max(delay * 2, step * 2, 1.0))
            return delay

    def _reset_penalty(self, host):
        with self._lock:
            self._backoff_step[host] = 1.0
            self._backoff_until.pop(host, None)

    def _read_cache(self, url):
        if not _cacheable(url):
            return None
        with self._lock:
            hit = self._cache.get(url)
            if hit is None or hit[0] <= self._clock():
                self._cache.pop(url, None)
                return None
            payload = hit[1]
        return copy.deepcopy(payload)

    def _write_cache(self, url, payload):
        with self._lock:
            self._cache[url] = (self._clock() + self._cache_seconds, copy.deepcopy(payload))


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.error = None


def _cacheable(url):
    parts = urlsplit(url)
    return parts.hostname == "gamma-api.polymarket.com" and "/markets/" in (parts.path or "")


_client = PublicClient()


def get_json(base, params=None):
    """GET public JSON through the shared client. No order is submitted."""
    return _client.get(base, params)


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
        delay = config["poll_seconds"]
        try:
            poll(config, journal)
            failures = 0
        except RateLimited as exc:
            if args.once:
                raise
            failures += 1
            delay = exc.retry_after
            print(json.dumps({"status": "ERROR", "message": str(exc),
                              "retry_after_seconds": exc.retry_after}), flush=True)
        except Exception as exc:
            if args.once:
                raise
            failures += 1
            delay = min(60, config["poll_seconds"] * (2 ** min(failures, 5)))
            print(json.dumps({"status": "ERROR", "message": str(exc)}), flush=True)
        if args.once:
            break
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
