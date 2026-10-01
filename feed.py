"""Public activity stream and reused HTTPS connections. Never submits orders.

The activity socket is Polymarket's public trade stream. Rows are filtered to
one wallet after they arrive. A dropped socket is the caller's signal to read
the same public activity API directly, without an idle poll.
"""
import base64
import http.client
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from urllib.parse import quote as urlquote
from urllib.parse import urlsplit, urlencode

from bot import array, decimal as D


HOST = "ws-live-data.polymarket.com"
PRICE_KEY = Decimal("0.000001")
SIZE_KEY = Decimal("0.0001")


def fill_key(row):
    """Identity shared by a stream trade and the same public activity row."""
    return (
        str(row.get("transaction_hash") or "").lower(),
        str(row.get("side") or ""),
        str(row.get("token_id") or ""),
        str(D(row["price"]).quantize(PRICE_KEY)),
        str(D(row["size"]).quantize(SIZE_KEY)),
    )


def row_from_activity(payload):
    """Map one public activity trade onto the fields the paper journal reads."""
    if not isinstance(payload, dict):
        raise ValueError("activity payload is not an object")
    timestamp = int(payload["timestamp"])
    if timestamp > 10**12:
        timestamp //= 1000
    size, price = payload["size"], payload["price"]
    usdc = payload.get("usdcSize", payload.get("usdc_size"))
    if usdc is None:
        usdc = float(D(size) * D(price))
    row = {
        "proxy_wallet": payload["proxyWallet"],
        "transaction_hash": payload["transactionHash"],
        "condition_id": payload["conditionId"],
        "token_id": str(payload["asset"]),
        "timestamp": timestamp,
        "side": payload["side"],
        "size": size,
        "price": price,
        "usdc_size": usdc,
        "type": "TRADE",
        "slug": payload["slug"],
        "outcome": payload["outcome"],
        "is_combo": False,
    }
    if not row["transaction_hash"] or row["side"] not in ("BUY", "SELL"):
        raise ValueError("activity trade is missing a hash or side")
    if not row["slug"] or not row["token_id"]:
        raise ValueError("activity trade is missing a market")
    return row


def match_trade(payload, wallet):
    """Return his trade, or None when the public stream trade is someone else's."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("proxyWallet", "")).lower() != str(wallet).lower():
        return None
    try:
        return row_from_activity(payload)
    except (ValueError, KeyError, TypeError):
        return None


def take_frames(buf):
    """Split a server buffer into complete frames. Returns frames and the remainder."""
    frames = []
    while len(buf) >= 2:
        first, second = buf[0], buf[1]
        masked = (second & 0x80) != 0
        length = second & 0x7F
        index = 2
        if length == 126:
            if len(buf) < 4:
                break
            length = struct.unpack("!H", buf[2:4])[0]
            index = 4
        elif length == 127:
            if len(buf) < 10:
                break
            length = struct.unpack("!Q", buf[2:10])[0]
            index = 10
        if length > 2_000_000:
            raise ValueError("websocket frame is too large")
        if masked:
            if len(buf) < index + 4:
                break
            mask = buf[index:index + 4]
            index += 4
        else:
            mask = b""
        if len(buf) < index + length:
            break
        payload = buf[index:index + length]
        buf = buf[index + length:]
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        frames.append(((first & 0x80) != 0, first & 0x0F, payload))
    return frames, buf


def client_frame(opcode, payload):
    mask = os.urandom(4)
    header = bytes([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return header + mask + masked


class JsonClient:
    """GET JSON over a thread-local keep-alive connection."""

    def __init__(self, timeout=4):
        self.timeout = timeout
        self._local = threading.local()

    def __call__(self, base, params=None):
        return self.get_json(base, params)

    def get_json(self, base, params=None):
        parts = urlsplit(base)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("json client only reads https")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if params:
            path += ("&" if "?" in path else "?") + urlencode(params)
        host = parts.hostname
        port = parts.port or 443
        key = (host, port)
        error = None
        for _ in range(2):
            conn = self._open(key, host, port)
            try:
                conn.request("GET", path, headers={
                    "Host": host,
                    "User-Agent": "btc-copy-paper-prototype/0.1",
                    "Accept": "application/json",
                    "Connection": "keep-alive",
                })
                response = conn.getresponse()
                body = response.read()
                if response.status != 200:
                    self._drop(key)
                    raise ValueError(f"http_{response.status}")
                return json.loads(body)
            except ValueError:
                raise
            except Exception as exc:
                self._drop(key)
                error = exc
        raise error

    def _open(self, key, host, port):
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        conn = conns.get(key)
        if conn is None:
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout)
            conn.connect()
            try:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            conns[key] = conn
        return conn

    def _drop(self, key):
        conns = getattr(self._local, "conns", None) or {}
        conn = conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


class QuoteCache:
    """Recent public book and market quotes, keyed by token."""

    def __init__(self):
        self._entries = {}
        self._lock = threading.Lock()

    def store(self, token, market, book, fetched_at):
        self._lock.acquire()
        try:
            self._entries[str(token)] = (fetched_at, market, book)
        finally:
            self._lock.release()

    def take(self, token, slug, now, max_age):
        """Return (market, book, age_seconds) when the quote is still young."""
        self._lock.acquire()
        try:
            item = self._entries.get(str(token))
        finally:
            self._lock.release()
        if item is None:
            return None
        fetched_at, market, book = item
        age = now - fetched_at
        if age < 0 or age > max_age or market.get("slug") != slug:
            return None
        return market, book, age


class BookPrefetcher:
    """Keep current BTC books and any open paper position warm. No orders."""

    def __init__(self, client, cache):
        self.client = client
        self.cache = cache
        self.stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._open = {}

    def set_open(self, pairs):
        """Remember (token, slug) pairs whose books a sell may need."""
        with self._lock:
            self._open = {str(token): slug for token, slug in pairs if token and slug}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="book-prefetch", daemon=True)
        self._thread.start()

    def close(self):
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.refresh()
            except Exception:
                pass
            remaining = 0.2 - (time.monotonic() - started)
            if remaining > 0 and self.stop.wait(remaining):
                return

    def refresh(self):
        now = int(time.time())
        with self._lock:
            open_slugs = set(self._open.values())
        slugs = {
            "btc-updown-5m-" + str(now - now % 300),
            "btc-updown-15m-" + str(now - now % 900),
        }
        slugs.update(slug for slug in open_slugs if slug)
        with ThreadPoolExecutor(max_workers=4) as pool:
            markets = {}
            requests = {
                pool.submit(self.client, "https://gamma-api.polymarket.com/markets/slug/" + urlquote(slug, safe="")): slug
                for slug in slugs
            }
            for job in as_completed(requests):
                try:
                    markets[requests[job]] = job.result()
                except Exception:
                    continue
            books = {}
            for market in markets.values():
                if not isinstance(market, dict) or not market.get("clobTokenIds"):
                    continue
                for token in array(market["clobTokenIds"]):
                    books[pool.submit(self.client, "https://clob.polymarket.com/book", {"token_id": token})] = (token, market)
            for job in as_completed(books):
                token, market = books[job]
                try:
                    book = job.result()
                except Exception:
                    continue
                self.cache.store(token, market, book, time.time())


class ActivityWatch:
    """Queue his trades from the public activity stream as they arrive."""

    def __init__(self, wallet, host=HOST, on_trade=None):
        self.wallet = wallet
        self.host = host
        self.on_trade = on_trade
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.errors = []
        self._thread = None
        self._sock = None
        self._lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="activity-watch", daemon=True)
        self._thread.start()

    def close(self):
        self.stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self):
        while not self.stop.is_set():
            try:
                self._session()
            except Exception as exc:
                self.connected.clear()
                if len(self.errors) < 20:
                    self.errors.append(str(exc))
                if self.stop.wait(0.5):
                    return

    def _session(self):
        raw = socket.create_connection((self.host, 443), timeout=10)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        sock.settimeout(10)
        with self._lock:
            self._sock = sock
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET / HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: btc-copy-paper-prototype/0.1\r\n\r\n"
            )
            sock.sendall(request.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("websocket handshake closed")
                buf += chunk
            head, _, buf = buf.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0]
            if b" 101 " not in status:
                raise ConnectionError(status.decode("ascii", "replace"))
            subscribe = json.dumps({"action": "subscribe", "subscriptions": [
                {"topic": "activity", "type": "trades", "filters": ""},
            ]})
            sock.sendall(client_frame(1, subscribe.encode()))
            self.connected.set()
            next_ping = time.monotonic() + 5
            fragments = []
            while not self.stop.is_set():
                if time.monotonic() >= next_ping:
                    sock.sendall(client_frame(1, b"ping"))
                    next_ping = time.monotonic() + 5
                frames, buf = take_frames(buf)
                if not frames:
                    sock.settimeout(1)
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("websocket closed")
                    buf += chunk
                    continue
                for fin, opcode, payload in frames:
                    if opcode == 8:
                        raise ConnectionError("websocket closed by server")
                    if opcode == 9:
                        sock.sendall(client_frame(0xA, payload))
                        continue
                    if opcode == 0xA:
                        continue
                    if opcode in (0, 1):
                        if opcode == 1:
                            fragments = [payload]
                        else:
                            fragments.append(payload)
                        if not fin:
                            continue
                        self._text(b"".join(fragments))
                        fragments = []
        finally:
            self.connected.clear()
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                if self._sock is sock:
                    self._sock = None

    def _text(self, payload):
        text = payload.decode("utf-8", "replace")
        if text in ("ping", "pong"):
            return
        if "payload" not in text:
            return
        try:
            message = json.loads(text)
            row = match_trade(message.get("payload"), self.wallet)
        except (ValueError, KeyError, TypeError):
            return
        if row is not None:
            arrived = time.time()
            prepared = None
            if self.on_trade:
                try:
                    prepared = self.on_trade(row)
                except Exception:
                    prepared = None
            self.queue.put((arrived, row, "activity_websocket", prepared))
