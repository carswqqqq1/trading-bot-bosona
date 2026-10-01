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
from email.utils import parsedate_to_datetime
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
    """A host returned 429. Further requests wait out retry_after and are not sent."""

    def __init__(self, host, retry_after):
        self.host = host
        self.retry_after = float(retry_after)
        super().__init__(f"HTTP 429 from {host}; backing off {self.retry_after:.3f}s")


class HttpStatus(Exception):
    def __init__(self, status, url):
        self.status = status
        super().__init__(f"HTTP {status} for {url}")


def _split_url(base, params):
    parts = urlsplit(base)
    path = parts.path or "/"
    query = urlencode(params) if params else ""
    if parts.query and query:
        path += "?" + parts.query + "&" + query
    elif parts.query or query:
        path += "?" + (parts.query or query)
    return parts.hostname, path


def _is_market_url(url):
    """Gamma market documents are cacheable. Books and activity are not."""
    return "gamma-api.polymarket.com/markets" in url


def _retry_after(headers, fallback):
    raw = None
    if headers:
        raw = headers.get("retry-after")
    if raw is None:
        return fallback
    try:
        return min(60.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return min(60.0, max(0.0, when.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return fallback


class _Connection:
    """One keep-alive connection. Tests can substitute a fake with the same methods."""

    def __init__(self, host, timeout=10):
        self._conn = http.client.HTTPSConnection(host, timeout=timeout)

    def request(self, method, path, headers=None):
        self._conn.request(method, path, headers=headers or {})

    def getresponse(self):
        response = self._conn.getresponse()
        self.status = response.status
        self._headers = {key.lower(): value for key, value in response.getheaders()}
        self._body = response.read()
        return self

    def read(self):
        return self._body

    def getheaders(self):
        return list(self._headers.items())

    def close(self):
        self._conn.close()


class PublicClient:
    """Process-wide public GET client. No orders and no keys.

    One connection per host is reused. A 429 arms a host backoff, and every
    later call during that window raises without sending. Gamma market
    documents are cached. Order books stay uncached so a copy still sees the
    current price.
    """

    def __init__(self, opener=None, connector=None, clock=None, market_ttl=30.0):
        self._opener = opener
        self._connector = connector or _Connection
        self._clock = clock or time.monotonic
        self.market_ttl = market_ttl
        self._lock = threading.Lock()
        self._host_locks = {}
        self._conns = {}
        self._backoff_until = {}
        self._backoff_step = {}
        self._markets = {}
        self._inflight = {}

    def retry_after(self, host):
        with self._lock:
            return max(0.0, self._backoff_until.get(host, 0.0) - self._clock())

    def get_json(self, base, params=None):
        host, path = _split_url(base, params)
        if not host:
            raise ValueError("missing host")
        url = "https://" + host + path
        with self._lock:
            cached = self._market_hit(url)
            if cached is not None:
                return cached
            retry = self._retry_locked(host)
            if retry > 0:
                raise RateLimited(host, retry)
            slot = self._inflight.get(url)
            if slot is None:
                slot = {"event": threading.Event(), "result": None, "error": None}
                self._inflight[url] = slot
                owner = True
            else:
                owner = False
        if not owner:
            if not slot["event"].wait(15):
                raise TimeoutError("timed out waiting for shared public request")
            if slot["error"] is not None:
                raise slot["error"]
            return copy.deepcopy(slot["result"])
        try:
            status, headers, body = self._send(host, path)
            if status == 429:
                delay = self._note_429(host, headers)
                raise RateLimited(host, delay)
            if status != 200:
                self._drop(host)
                raise HttpStatus(status, url)
            try:
                data = json.loads(body.decode() if isinstance(body, bytes) else body)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("Unexpected public API response") from exc
            if _is_market_url(url):
                self._store_market(url, data)
            else:
                self._note_success(host)
            slot["result"] = data
            return copy.deepcopy(data)
        except Exception as exc:
            slot["error"] = exc
            raise
        finally:
            slot["event"].set()
            with self._lock:
                self._inflight.pop(url, None)

    def _market_hit(self, url):
        if not _is_market_url(url):
            return None
        cached = self._markets.get(url)
        if cached is None or cached[0] <= self._clock():
            self._markets.pop(url, None)
            return None
        return copy.deepcopy(cached[1])

    def _store_market(self, url, data):
        with self._lock:
            self._markets[url] = (self._clock() + self.market_ttl, copy.deepcopy(data))
            self._backoff_step[urlsplit(url).hostname] = 1.0

    def _retry_locked(self, host):
        return max(0.0, self._backoff_until.get(host, 0.0) - self._clock())

    def _note_429(self, host, headers):
        with self._lock:
            step = self._backoff_step.get(host, 1.0)
            delay = _retry_after(headers, step)
            self._backoff_until[host] = self._clock() + delay
            self._backoff_step[host] = min(max(step, 1.0) * 2, 30.0)
            return delay

    def _note_success(self, host):
        with self._lock:
            self._backoff_step[host] = 1.0
            self._backoff_until.pop(host, None)

    def _host_lock(self, host):
        with self._lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def _drop(self, host):
        with self._lock:
            conn = self._conns.pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _send(self, host, path):
        if self._opener is not None:
            return self._opener(host, path)
        lock = self._host_lock(host)
        with lock:
            try:
                return self._send_once(host, path)
            except RateLimited:
                raise
            except HttpStatus:
                raise
            except Exception:
                self._drop(host)
                return self._send_once(host, path)

    def _send_once(self, host, path):
        with self._lock:
            conn = self._conns.get(host)
            if conn is None:
                conn = self._connector(host, timeout=10)
                self._conns[host] = conn
        try:
            conn.request("GET", path, headers={
                "User-Agent": "btc-copy-paper-prototype/0.1",
                "Accept": "application/json",
                "Connection": "keep-alive",
            })
            response = conn.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers, body
        except Exception:
            self._drop(host)
            raise


public_client = PublicClient()


def get_json(base, params=None):
    return public_client.get_json(base, params)


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
            delay = max(delay, exc.retry_after)
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
