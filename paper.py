"""Persistent cash-and-inventory paper copy test using public executable depth.

No orders are submitted. Fees use Gamma feeSchedule and the documented formula
https://docs.polymarket.com/trading/fees (verified 2026-09-30). No settlement.
SELL fraction uses source holdings observed during this session only; earlier
leader inventory is unknown and unmatched SELLs are refused.

BUY copies keep his side and market only when his fill is inside the configured
price band. The paper size is his share count, not a smaller clip. The market
minimum is still 5 shares, so a smaller print is skipped. The book must fill
that full size at his price or better. If the full cost is more than cash, the
buy is skipped and is not scaled down. A paper position is sold only inside the
same window as his fill when the bid is above its average cost and the sale
nets a gain. The runner decides when his public trade frame arrives. These
rules do not guarantee a profit.
"""
import argparse
import base64
import http.client
import json
import math
import os
import queue
import re
import socket
import sqlite3
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from urllib.parse import quote as urlquote, urlencode, urlsplit

from bot import activity, array, decimal as D, get_json, market_timeframe, row_keys, source_skip, validate

ZERO = Decimal(0)
STEP = Decimal('0.01')
FIVE = Decimal('5')


def ordered_stamp(row):
    try:
        return int(row.get('timestamp') or 0)
    except (TypeError, ValueError):
        return None


def fresh_trades(rows, seen, config, observer_start, preserve_order=False):
    """Unseen leader trades, newest fill first.

    Public activity arrives as a page. Deciding in this order means an older
    row in that page cannot delay the newest fill. Keys still come from
    row_keys, so they stay aligned with the startup baseline.
    """
    selected = []
    for key, row in row_keys(rows):
        if seen(key):
            continue
        if (row.get('proxy_wallet', '').lower() != config['leader_wallet'].lower()
                or row.get('type') != 'TRADE' or row.get('is_combo')
                or row.get('side') not in ('BUY', 'SELL')
                or market_timeframe(row.get('slug')) not in config['timeframes_minutes']):
            continue
        stamp = ordered_stamp(row)
        if stamp is None or stamp < observer_start:
            continue
        selected.append((stamp, key, row))
    if not preserve_order:
        selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(key, row) for _, key, row in selected]


def outside_copy_band(row, config):
    """BUY prices outside [copy_price_min, copy_price_max] are not copied."""
    if row.get('side') != 'BUY' or 'copy_price_min' not in config:
        return False
    price = D(row['price'])
    return not D(config['copy_price_min']) <= price <= D(config['copy_price_max'])


def activity_pages_newest(wallet, start, end, fetch=get_json):
    """Yield public activity pages newest-first so the first page can be acted on."""
    params = dict(user=wallet, type='TRADE', start=start, end=end,
                  limit=100, sort_direction='DESC')
    cursors = set()
    for _ in range(100):
        page = fetch('https://data-api.polymarket.com/v2/activity', params)
        if not isinstance(page.get('data'), list):
            raise ValueError('Unexpected activity API response')
        yield page['data']
        pagination = page.get('pagination') or {}
        if not pagination.get('has_more'):
            return
        cursor = pagination.get('next_cursor')
        if not cursor or cursor in cursors:
            raise ValueError('Missing or repeating activity cursor')
        cursors.add(cursor)
        params['cursor'] = cursor


_HTTP = threading.local()


def paper_get_json(base, params=None):
    """GET JSON on a reused TLS connection. Paper reads only; no orders."""
    parts = urlsplit(base)
    if parts.scheme != 'https' or not parts.hostname:
        return get_json(base, params)
    path = parts.path or '/'
    query = urlencode(params) if params else parts.query
    if query:
        path += '?' + query
    last = None
    for _ in range(2):
        conn = _https_conn(parts.hostname)
        try:
            conn.request('GET', path, headers={
                'User-Agent': 'btc-copy-paper-prototype/0.1',
                'Connection': 'keep-alive',
                'Accept': 'application/json',
            })
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                raise ValueError('http_%s' % response.status)
            return json.loads(body)
        except Exception as exc:
            last = exc
            _drop_https_conn(parts.hostname)
    raise last


def _https_conn(host):
    """One TLS connection per thread. A shared connection can stall the book read."""
    local = getattr(_HTTP, 'local', None)
    if local is None:
        local = threading.local()
        _HTTP.local = local
    conns = getattr(local, 'conns', None)
    if conns is None:
        conns = {}
        local.conns = conns
    conn = conns.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(host, timeout=4)
        conns[host] = conn
    return conn


def _drop_https_conn(host):
    local = getattr(_HTTP, 'local', None)
    conns = getattr(local, 'conns', None) if local is not None else None
    if not conns:
        return
    conn = conns.pop(host, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _ready(fut, seconds=5):
    """Bound a public book or market read so one stall cannot freeze decisions."""
    return fut.result(timeout=seconds)


def stream_trade_row(payload, wallet):
    """Map one public activity/trades payload into the paper row shape."""
    if not isinstance(payload, dict):
        return None
    proxy = payload.get('proxyWallet') or payload.get('proxy_wallet')
    if not isinstance(proxy, str) or proxy.lower() != wallet.lower():
        return None
    side = payload.get('side')
    if side not in ('BUY', 'SELL'):
        return None
    try:
        stamp = int(payload['timestamp'])
    except (TypeError, ValueError, KeyError):
        return None
    if stamp > 10**12:
        stamp //= 1000
    return dict(proxy_wallet=proxy, transaction_hash=payload.get('transactionHash') or payload.get('transaction_hash'),
                condition_id=payload.get('conditionId') or payload.get('condition_id'),
                token_id=payload.get('asset') or payload.get('token_id'),
                timestamp=stamp, side=side, size=payload.get('size'), price=payload.get('price'),
                usdc_size=payload.get('usdcSize', payload.get('usdc_size')), type='TRADE',
                slug=payload.get('slug'), outcome=payload.get('outcome'), is_combo=False)


def active_btc_slugs(now):
    """Current and next BTC windows. Used only to warm public market metadata."""
    from zoneinfo import ZoneInfo
    now = int(now)
    slugs = []
    for step, name in ((300, '5m'), (900, '15m')):
        start = now // step * step
        slugs.append(f'btc-updown-{name}-{start}')
        slugs.append(f'btc-updown-{name}-{start + step}')
    et = datetime.fromtimestamp(now, ZoneInfo('America/New_York'))
    hour = int(et.strftime('%I'))
    slugs.append(
        f"bitcoin-up-or-down-{et.strftime('%B').lower()}-{et.day}-{et.year}-{hour}{et.strftime('%p').lower()}-et"
    )
    return slugs


class MarketCache:
    """Keep Gamma metadata for the open BTC windows so a fill does not wait on it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.markets = {}
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name='paper-market-cache', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()

    def get(self, slug, max_age=120):
        with self.lock:
            item = self.markets.get(slug)
        if not item or time.time() - item[0] > max_age:
            return None
        return item[1]

    def _run(self):
        while not self.stop.is_set():
            for slug in active_btc_slugs(time.time()):
                if self.stop.is_set():
                    return
                try:
                    market = paper_get_json('https://gamma-api.polymarket.com/markets/slug/' + urlquote(slug, safe=''))
                except Exception:
                    continue
                if isinstance(market, dict) and market.get('slug') == slug:
                    with self.lock:
                        self.markets[slug] = (time.time(), market)
                    self.ready.set()
            self.stop.wait(1.5)


class PublicTradeFeed:
    """Push one leader's public trades as they arrive. Never sends an order."""

    def __init__(self, wallet, prepare=None):
        self.wallet = wallet
        self.prepare = prepare
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='paper-trade-feed', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()

    def _run(self):
        while not self.stop.is_set():
            try:
                self._session()
            except Exception as exc:
                self.queue.put(('error', str(exc)))
                self.stop.wait(1)

    def _session(self):
        host = 'ws-live-data.polymarket.com'
        raw = socket.create_connection((host, 443), timeout=10)
        try:
            ssock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode()
            ssock.sendall((
                'GET / HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (host, key)
            ).encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = ssock.recv(4096)
                if not chunk:
                    raise ConnectionError('websocket_handshake_closed')
                buf += chunk
            head, buf = buf.split(b'\r\n\r\n', 1)
            if b' 101 ' not in head.split(b'\r\n', 1)[0]:
                raise ConnectionError('websocket_handshake_rejected')
            self._send(ssock, 1, json.dumps({'action': 'subscribe', 'subscriptions': [
                {'topic': 'activity', 'type': 'trades'}]}).encode())
            ssock.settimeout(1)
            last_ping = time.time()
            last_frame = time.time()
            fragments = []
            while not self.stop.is_set():
                if time.time() - last_frame > 15:
                    raise ConnectionError('websocket_silent')
                if time.time() - last_ping >= 5:
                    self._send(ssock, 1, b'PING')
                    last_ping = time.time()
                try:
                    fin, opcode, payload, buf = self._read_frame(ssock, buf)
                except socket.timeout:
                    buf = getattr(self, '_pending', buf)
                    continue
                last_frame = time.time()
                if opcode == 8:
                    raise ConnectionError('websocket_closed')
                if opcode == 9:
                    self._send(ssock, 10, payload)
                    continue
                if opcode != 1 and opcode != 0:
                    continue
                if opcode == 1:
                    fragments = [payload]
                else:
                    fragments.append(payload)
                if not fin:
                    continue
                payload = b''.join(fragments)
                fragments = []
                if not payload or payload == b'PONG':
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                body = msg.get('payload') if isinstance(msg, dict) else None
                row = stream_trade_row(body, self.wallet)
                if row is not None:
                    seen_at = time.time()
                    if self.prepare is not None:
                        try:
                            self.prepare(row)
                        except Exception:
                            pass
                    self.queue.put(('trade', seen_at, row))
        finally:
            try:
                raw.close()
            except Exception:
                pass

    def _send(self, ssock, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', n))
        ssock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read_frame(self, ssock, buf):
        """Read one frame. A timeout keeps bytes already taken from the socket."""
        try:
            while len(buf) < 2:
                buf += self._recv(ssock)
            length = buf[1] & 0x7f
            index = 2
            if length == 126:
                while len(buf) < 4:
                    buf += self._recv(ssock)
                length = struct.unpack('!H', buf[2:4])[0]
                index = 4
            elif length == 127:
                while len(buf) < 10:
                    buf += self._recv(ssock)
                length = struct.unpack('!Q', buf[2:10])[0]
                index = 10
            if length > 1_000_000:
                raise ConnectionError('websocket_frame_too_large')
            fin = bool(buf[0] & 0x80)
            masked = buf[1] & 0x80
            if masked:
                while len(buf) < index + 4:
                    buf += self._recv(ssock)
                mask = buf[index:index+4]
                index += 4
            while len(buf) < index + length:
                buf += self._recv(ssock)
        except socket.timeout:
            self._pending = buf
            raise
        payload = buf[index:index+length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, buf[0] & 0x0f, payload, buf[index+length:]

    def _recv(self, ssock):
        chunk = ssock.recv(65536)
        if not chunk:
            raise ConnectionError('socket_closed')
        return chunk


def fee_rate(market):
    if market.get('feesEnabled') is False:
        return ZERO
    schedule = market.get('feeSchedule') or {}
    if (market.get('feesEnabled') is not True or schedule.get('exponent') != 1
            or schedule.get('takerOnly') is not True):
        raise ValueError('unknown_fee_schedule')
    rate = D(schedule['rate'])
    if not 0 <= rate <= 1:
        raise ValueError('invalid_fee_rate')
    return rate


def levels(book, side, limit=None):
    result = []
    for entry in book['asks' if side == 'BUY' else 'bids']:
        price, size = D(entry['price']), D(entry['size'])
        if not 0 < price < 1 or size < 0:
            raise ValueError('invalid_book_level')
        if limit is None or (price <= limit if side == 'BUY' else price >= limit):
            result.append((price, size))
    return sorted(result, reverse=side == 'SELL')


def quote(book, side, quantity, rate, limit=None):
    """Full depth fill only; fees round per consumed price level to five decimals."""
    if side not in ('BUY', 'SELL') or quantity <= 0:
        raise ValueError('invalid_quantity_or_side')
    remaining, gross, fees = quantity, ZERO, ZERO
    for price, available in levels(book, side, limit):
        take = min(remaining, available)
        if take <= 0:
            continue
        gross += take * price
        fees += (take * rate * price * (1-price)).quantize(Decimal('.00001'), rounding=ROUND_HALF_UP)
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise ValueError('insufficient_depth')
    return dict(shares=quantity, gross=gross, fee=fees, vwap=gross/quantity)


def buy_entry(book, source_price, rate, tick, minimum, cash, his_shares, worse_than_leader=ZERO):
    """Buy his exact share count, or raise a rule name.

    The market minimum is still 5 shares. A smaller print is skipped.
    The book must sell that full size at his price or better. If the fee-inclusive
    cost is more than cash, the buy is skipped. The size is never reduced to fit.
    Passing does not guarantee a profit. The copied market and side are his.
    """
    floor = max(minimum, FIVE)
    if his_shares < floor:
        raise ValueError('below_market_minimum')
    ceiling = source_price + worse_than_leader
    asks = levels(book, 'BUY')
    if not asks:
        raise ValueError('shares_not_at_or_better_than_leader_fill')
    if asks[0][0] > ceiling:
        raise ValueError('latency_worse_than_leader_price')
    limit = (ceiling / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    try:
        preview = quote(book, 'BUY', his_shares, rate, limit)
    except ValueError:
        raise ValueError('shares_not_at_or_better_than_leader_fill')
    if preview['vwap'] > ceiling:
        raise ValueError('latency_worse_than_leader_price')
    if preview['gross'] + preview['fee'] > cash:
        raise ValueError('his_size_exceeds_cash')
    return his_shares, limit


def market_check(row, market, book, config, now):
    adapted = dict(row, side='BUY')
    reason = source_skip(adapted, config, now)
    if reason:
        return reason
    if market.get('slug') != row['slug'] or market.get('conditionId') != row['condition_id']:
        return 'market_identity_mismatch'
    if (market.get('active') is not True or market.get('closed') is not False
            or market.get('acceptingOrders') is not True or market.get('archived') is True):
        return 'market_not_tradable'
    tokens, outcomes = array(market['clobTokenIds']), array(market['outcomes'])
    token = str(row['token_id'])
    if (len(tokens) != 2 or len(set(tokens)) != 2 or len(outcomes) != 2
            or set(outcomes) != {'Up', 'Down'} or token not in tokens
            or outcomes[tokens.index(token)] != row.get('outcome')):
        return 'outcome_token_mismatch'
    if book.get('asset_id') != token or book.get('market') != row['condition_id']:
        return 'book_identity_mismatch'
    age = D(now)*1000-D(book['timestamp'])
    if age < -1000 or age > D(config['max_book_age_seconds'])*1000:
        return 'stale_or_future_book'
    end = datetime.fromisoformat(market['endDate'].replace('Z', '+00:00'))
    if end.tzinfo is None:
        return 'market_expiry_missing_timezone'
    match = re.fullmatch(r'btc-updown-(5|15)m-(\d+)', row['slug'])
    if match:
        start = int(match[2])
        expiry = min(end.timestamp(), start+int(match[1])*60)
    else:
        start_date = datetime.fromisoformat(market['eventStartTime'].replace('Z', '+00:00'))
        if start_date.tzinfo is None or end.timestamp()-start_date.timestamp() != 3600:
            return 'unsupported_hourly_interval'
        start, expiry = start_date.timestamp(), end.timestamp()
    if now < start or expiry-now < config['min_seconds_to_expiry']:
        return 'market_not_started_or_near_expiry'
    return None


class PaperJournal:
    """Callers provide validated config; the CLI validates numeric risk limits."""

    def __init__(self, path, config):
        self.config = config
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY,payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS positions (token TEXT PRIMARY KEY,payload TEXT)')
        encoded = json.dumps(config, sort_keys=True)
        stored = self.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if stored and stored[0] != encoded:
            raise ValueError('Configuration changed: use a fresh --db file')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('config',?)", (encoded,))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('cash',?)", (str(config['starting_cash_usd']),))

    @property
    def cash(self):
        return D(self.db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])

    def holdings(self):
        return {token: dict(json.loads(payload), **{k:D(json.loads(payload)[k]) for k in ('shares','cost','leader_shares')})
                for token,payload in self.db.execute('SELECT token,payload FROM positions')}

    def contains(self, key):
        return self.db.execute('SELECT 1 FROM seen WHERE id=?', (key,)).fetchone() is not None

    def baseline(self, rows, observer_start):
        if self.db.execute("SELECT 1 FROM meta WHERE key='observer_start'").fetchone():
            return
        with self.db:
            self.db.execute("INSERT INTO meta VALUES ('observer_start',?)", (str(observer_start),))
            self.db.executemany('INSERT OR IGNORE INTO seen VALUES (?,?)',
                                [(key,json.dumps({'status':'BASELINE'})) for key,_ in row_keys(rows)])

    def process(self, key, row, market, book, now, skip_reason=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.contains(key):
                return {'status':'DUPLICATE'}
            positions = self.holdings()
            token = str(row['token_id'])
            position = positions.get(token, dict(row=row, shares=ZERO,cost=ZERO,leader_shares=ZERO))
            source_shares = D(row['size'])
            if source_shares <= 0 or row['side'] not in ('BUY','SELL'):
                raise ValueError('invalid_source_trade')
            leader_before = position['leader_shares']
            # Track observed source inventory even when a copy is skipped.
            position['leader_shares'] = (leader_before+source_shares if row['side']=='BUY'
                                         else max(ZERO,leader_before-source_shares))
            source_stamp = int(row['timestamp'])
            decision = dict(status='SKIP',event_id=key,paper=True,executed=False,side=row['side'],slug=row['slug'],
                            outcome=row['outcome'],source_transaction=row.get('transaction_hash'),
                            source_timestamp_seconds=row['timestamp'],
                            source_to_decision_seconds=round(now-source_stamp,3),
                            decision_latency_seconds=round(now-source_stamp,3),
                            his_fill_time_utc=datetime.fromtimestamp(source_stamp,timezone.utc).isoformat(),
                            paper_action_time_utc=datetime.fromtimestamp(now,timezone.utc).isoformat())
            try:
                try:
                    parsed = D(row['price'])
                    if 0 < parsed < 1:
                        decision['source_price'] = str(parsed)
                        decision['his_price'] = str(parsed)
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    pass
                if skip_reason:
                    raise ValueError(skip_reason)
                reason = market_check(row,market,book,self.config,now)
                if reason:
                    raise ValueError(reason)
                rate = fee_rate(market)
                minimum = D(book['min_order_size'])
                tick = D(book['tick_size'])
                if minimum <= 0 or tick <= 0:
                    raise ValueError('invalid_market_constraints')
                drift = D(self.config['max_price_drift'])
                source_price = D(row['price'])
                if not 0 < source_price < 1:
                    raise ValueError('invalid_source_price')
                # Record the current executable minimum-size price even when the
                # account cannot afford that minimum. This never changes funds.
                decision['source_price'] = str(source_price)
                try:
                    diagnostic = quote(book,row['side'],minimum,rate)
                    decision['minimum_size_price_comparison'] = dict(
                        shares=str(minimum),snapshot_vwap=str(diagnostic['vwap']),
                        snapshot_fee_usd=str(diagnostic['fee']),
                        source_price_slippage_cost_usd=str(
                            (diagnostic['vwap']-source_price)*minimum*(1 if row['side']=='BUY' else -1)),
                        executed=False,
                        interpretation='Hypothetical minimum-size quote, not an account fill or causal latency estimate.')
                except ValueError:
                    pass
                if row['side']=='BUY' and 'copy_price_min' in self.config:
                    if not (D(self.config['copy_price_min']) <= source_price <= D(self.config['copy_price_max'])):
                        raise ValueError('outside_copy_price_band')
                if row['side']=='BUY':
                    # His full size, or skip. Stored per-buy caps are not applied.
                    quantity, limit = buy_entry(
                        book, source_price, rate, tick, minimum, self.cash, source_shares,
                        D(self.config.get('max_worse_than_leader', 0)))
                else:
                    if not leader_before or source_shares > leader_before:
                        raise ValueError('unmatched_source_sell_baseline_inventory_unknown')
                    quantity = (position['shares']*source_shares/leader_before).quantize(STEP,rounding=ROUND_DOWN)
                    limit = max(ZERO,source_price-drift)
                if quantity < minimum or quantity <= 0:
                    raise ValueError('below_market_minimum_or_budget_cap')
                fill = quote(book,row['side'],quantity,rate,limit)
                if row['side']=='BUY':
                    debit = fill['gross']+fill['fee']
                    if debit > self.cash:
                        raise ValueError('his_size_exceeds_cash')
                    cash = self.cash-debit
                    position['shares'] += quantity
                    position['cost'] += debit
                    position['entry_source_timestamp'] = source_stamp
                    position['entry_price'] = str(source_price)
                else:
                    cash = self.cash+fill['gross']-fill['fee']
                    removed_cost = position['cost'] * quantity / position['shares']
                    position['cost'] -= removed_cost
                    decision['realized_pnl_usd'] = str(fill['gross']-fill['fee']-removed_cost)
                    position['shares'] -= quantity
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                slippage = (fill['vwap']-source_price)*quantity*(1 if row['side']=='BUY' else -1)
                decision.update(source_price=str(source_price),simulated_vwap=str(fill['vwap']),
                                source_price_slippage_cost_usd=str(slippage),
                                source_price_fee_estimate_usd=str((quantity*rate*source_price*(1-source_price)).quantize(Decimal('.00001'),rounding=ROUND_HALF_UP)))
                decision['fee_delta_vs_hypothetical_source_price_usd'] = str(fill['fee']-D(decision['source_price_fee_estimate_usd']))
                decision.update(status='PAPER_'+row['side'],**{k:str(v) for k,v in fill.items()},
                                fee_rate=str(rate),cash_usd=str(cash),held_shares=str(position['shares']))
            except (ValueError, KeyError, TypeError) as exc:
                decision['reason'] = str(exc)
            decision['rule_skipped'] = decision['status'] == 'SKIP'
            decision['cash_usd'] = decision.get('cash_usd', str(self.cash))
            running = self.realized_total()
            if decision.get('status') == 'PAPER_SELL' and decision.get('realized_pnl_usd') is not None:
                running += D(decision['realized_pnl_usd'])
            decision['running_realized_pnl_usd'] = str(running)
            our_price = decision.get('simulated_vwap')
            if our_price is None:
                our_price = (decision.get('minimum_size_price_comparison') or {}).get('snapshot_vwap')
            if our_price is not None and decision.get('source_price') is not None:
                decision['our_price'] = str(our_price)
                decision['his_price'] = str(decision['source_price'])
                # Positive means we would pay more than his fill. Same side and market.
                decision['cent_difference'] = str((D(our_price)-D(decision['source_price']))*100)
            position['row'] = row
            payload = json.dumps(position,default=str)
            self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',(token,payload))
            unrealized = self.mark_unrealized(market if isinstance(market, dict) else None,
                                              book if isinstance(book, dict) else None)
            if unrealized is not None:
                decision['unrealized_pnl_usd'] = str(unrealized)
            self.db.execute('INSERT INTO seen VALUES (?,?)',(key,json.dumps(decision)))
            return decision

    def realized_total(self):
        total = ZERO
        for (payload,) in self.db.execute('SELECT payload FROM seen'):
            item = json.loads(payload)
            if item.get('status') == 'PAPER_SELL':
                total += D(item.get('realized_pnl_usd', '0'))
        return total

    def mark_unrealized(self, market, book):
        """Liquidation P/L when every open token is covered by this book, else None."""
        positions = [(token, held) for token, held in self.holdings().items() if held['shares'] > 0]
        if not positions:
            return ZERO
        if not market or not book or len(positions) != 1:
            return None
        token, held = positions[0]
        if str(book.get('asset_id') or '') != token:
            return None
        try:
            if held['shares'] < D(book['min_order_size']):
                return None
            fill = quote(book, 'SELL', held['shares'], fee_rate(market))
        except (ValueError, KeyError, TypeError):
            return None
        return fill['gross'] - fill['fee'] - held['cost']

    def realize_if_bid_above_cost(self, market, book, now):
        """Sell paper shares when the bid is above average cost and nets a gain.

        This can realize a winner inside the window. It does not guarantee one.
        """
        token = str(book.get('asset_id') or '')
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                position = self.holdings().get(token)
                if not position or position['shares'] <= 0 or position['cost'] <= 0:
                    return None
                opened = position.get('entry_source_timestamp')
                window = self.config.get('exit_window_seconds')
                if window is not None and opened is not None and now - int(opened) > float(window):
                    return None
                if book.get('market') != position['row'].get('condition_id'):
                    return None
                age = D(now)*1000-D(book['timestamp'])
                if age < -1000 or age > D(self.config['max_book_age_seconds'])*1000:
                    return None
                rate = fee_rate(market)
                minimum = D(book['min_order_size'])
                if minimum <= 0:
                    return None
                cost_per = position['cost']/position['shares']
                bids = [level for level in levels(book,'SELL') if level[0] > cost_per]
                if not bids:
                    return None
                available = sum((size for _,size in bids), ZERO)
                quantity = min(position['shares'], available).quantize(STEP, rounding=ROUND_DOWN)
                if quantity < minimum or quantity <= 0:
                    return None
                fill = quote(book,'SELL',quantity,rate,cost_per)
                removed = position['cost']*quantity/position['shares']
                net = fill['gross']-fill['fee']
                if net <= removed:
                    return None
                cash = self.cash+net
                position['cost'] -= removed
                position['shares'] -= quantity
                if position['shares'] == 0:
                    position['cost'] = ZERO
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                opened_stamp = int(opened) if opened is not None else None
                decision = dict(status='PAPER_SELL',reason='bid_above_paper_cost',rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),our_price=str(fill['vwap']),
                                realized_pnl_usd=str(net-removed),
                                running_realized_pnl_usd=str(self.realized_total()+(net-removed)),
                                cash_usd=str(cash),held_shares=str(position['shares']),
                                cost_per_share_usd=str(cost_per))
                if opened_stamp is not None:
                    decision.update(his_fill_time_utc=datetime.fromtimestamp(opened_stamp,timezone.utc).isoformat(),
                                    paper_action_time_utc=datetime.fromtimestamp(now,timezone.utc).isoformat(),
                                    decision_latency_seconds=round(now-opened_stamp,3),
                                    source_timestamp_seconds=opened_stamp)
                if position.get('entry_price') is not None:
                    decision['his_price'] = str(position['entry_price'])
                    decision['cent_difference'] = str((fill['vwap']-D(position['entry_price']))*100)
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
                unrealized = self.mark_unrealized(market, book)
                if unrealized is not None:
                    decision['unrealized_pnl_usd'] = str(unrealized)
                self.db.execute('INSERT INTO seen VALUES (?,?)',
                                (f"exit:{token}:{now}:{quantity}",json.dumps(decision)))
                return decision
        except Exception:
            return None

    def portfolio(self, fetch=get_json):
        positions, marked, unknown = [],ZERO,0
        for token,p in self.holdings().items():
            if p['shares'] <= 0:
                continue
            record = dict(token_id=token,slug=p['row']['slug'],shares=str(p['shares']),cost_usd=str(p['cost']))
            try:
                market = fetch('https://gamma-api.polymarket.com/markets/slug/'+urlquote(p['row']['slug'],safe=''))
                book = fetch('https://clob.polymarket.com/book',{'token_id':token})
                row = dict(p['row'],timestamp=int(time.time()))
                reason = market_check(row,market,book,self.config,time.time())
                if reason:
                    raise ValueError(reason)
                if p['shares'] < D(book['min_order_size']):
                    raise ValueError('holding_below_sell_minimum')
                fill = quote(book,'SELL',p['shares'],fee_rate(market))
                value = fill['gross']-fill['fee']
                marked += value
                record['liquidation_quote_usd'] = str(value)
            except Exception as exc:
                unknown += 1
                record['unresolved_reason'] = str(exc)
            positions.append(record)
        fills = [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM seen')]
        fills = [x for x in fills if x['status'] in ('PAPER_BUY','PAPER_SELL')]
        realized = sum((D(x.get('realized_pnl_usd','0')) for x in fills),ZERO)
        open_cost = sum((p['cost'] for p in self.holdings().values()),ZERO)
        result = dict(status='PORTFOLIO',cash_usd=str(self.cash),positions=positions,
                      unresolved_positions=unknown,quoted_liquidation_usd=str(marked),settlement_simulated=False)
        result.update(total_simulated_fees_usd=str(sum((D(x.get('fee','0')) for x in fills),ZERO)),
                      source_price_slippage_cost_usd=str(sum((D(x.get('source_price_slippage_cost_usd','0')) for x in fills),ZERO)),
                      fee_delta_vs_hypothetical_source_price_usd=str(sum((D(x.get('fee_delta_vs_hypothetical_source_price_usd','0')) for x in fills),ZERO)),
                      hypothetical_source_fee_assumption='Same quantity and taker fee schedule at leader price; actual leader fees unknown.',
                      realized_pnl_usd=str(realized),open_cost_usd=str(open_cost),simulated_fills=len(fills),
                      slippage_interpretation='Price difference at observed snapshot; not a causal latency estimate.')
        if not unknown:
            result['unrealized_pnl_at_liquidation_quote_usd'] = str(marked-open_cost)
            equity = self.cash+marked
            result.update(equity_at_liquidation_quote_usd=str(equity),
                          change_from_start_usd=str(equity-D(self.config['starting_cash_usd'])))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.paper48.json')
    parser.add_argument('--db',default='paper48.sqlite3')
    parser.add_argument('--output',default='paper48.jsonl')
    parser.add_argument('--duration',type=float,default=120)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error('duration must be positive and finite')
    socket.setdefaulttimeout(8)
    raw_config = json.loads(Path(args.config).read_text())
    # Transport is not part of the stored strategy. Reusing a paper db must keep cash.
    transport = str(raw_config.pop('signal_transport', 'poll'))
    if transport not in ('poll', 'websocket'):
        raise ValueError('signal_transport must be poll or websocket')
    interval = float(D(raw_config['poll_seconds']))
    if not math.isfinite(interval) or interval < 0.1:
        raise ValueError('paper poll_seconds must be finite and at least 0.1')
    config = validate(dict(raw_config,poll_seconds=max(1,interval)))
    config['poll_seconds'] = interval
    for key in ('starting_cash_usd','max_buy_usd','max_open_cost_usd','max_outcome_cost_usd','max_book_age_seconds'):
        if D(config[key]) <= 0:
            raise ValueError(key+' must be positive')
    if 'target_buy_usd' in config and not 0 < D(config['target_buy_usd']) <= D(config['max_buy_usd']):
        raise ValueError('target_buy_usd must be positive and within max_buy_usd')
    if D(config['max_open_cost_usd']) >= D(config['starting_cash_usd']):
        raise ValueError('max_open_cost_usd must stay below starting cash')
    if 'max_worse_than_leader' in config and D(config['max_worse_than_leader']) < 0:
        raise ValueError('max_worse_than_leader cannot be negative')
    if 'copy_price_min' in config or 'copy_price_max' in config:
        if D(config['copy_price_min']) > D(config['copy_price_max']):
            raise ValueError('copy_price_min cannot exceed copy_price_max')
        if not 0 < D(config['copy_price_min']) or not D(config['copy_price_max']) < 1:
            raise ValueError('copy price band must lie inside (0, 1)')
    if 'exit_window_seconds' in config and D(config['exit_window_seconds']) <= 0:
        raise ValueError('exit_window_seconds must be positive')
    journal = PaperJournal(args.db,config)
    stored = journal.db.execute("SELECT value FROM meta WHERE key='observer_start'").fetchone()
    observer_start = int(stored[0]) if stored else int(time.time())
    source_start = observer_start-3600
    if not stored:
        journal.baseline(activity(config['leader_wallet'],source_start,int(time.time())),observer_start)
    started, run_started_wall, observations = time.monotonic(),time.time(),{}
    decision_latencies = []
    closed_windows = 0
    planned_windows = max(1, int(args.duration // 60))
    # Only long enough to still see a fill before max_signal_age_seconds rejects it.
    lookback = int(config['max_signal_age_seconds']) + 15
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=8) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)
        def note_decision(decision):
            if decision.get('status') == 'DUPLICATE':
                return
            latency = decision.get('decision_latency_seconds')
            stamp = decision.get('source_timestamp_seconds')
            if latency is None or stamp is None:
                return
            if int(stamp) >= run_started_wall and float(latency) >= 0 and decision.get('reason') != 'bid_above_paper_cost':
                decision_latencies.append(float(latency))
        def close_windows():
            nonlocal closed_windows
            elapsed = time.time()-run_started_wall
            while closed_windows < planned_windows and elapsed >= (closed_windows+1)*60:
                closed_windows += 1
                try:
                    snapshot = journal.portfolio(paper_get_json)
                except Exception as exc:
                    snapshot = dict(cash_usd=str(journal.cash), message=str(exc))
                snapshot.update(status='WINDOW',window=closed_windows,paper=True,executed=False,
                                window_end_utc=datetime.now(timezone.utc).isoformat())
                emit(snapshot)
        emit(dict(status='STARTED',starting_cash_usd=str(config['starting_cash_usd']),cash_usd=str(journal.cash),
                  paper=True,executed=False,observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  poll_seconds=config['poll_seconds'],activity_lookback_seconds=lookback,
                  copy_price_min=config.get('copy_price_min'),copy_price_max=config.get('copy_price_max'),
                  exit_window_seconds=config.get('exit_window_seconds'),
                  signal_transport=transport,
                  latency_policy=('Public trade stream. The book read starts inside the frame handler, before the decision thread wakes, using a warm connection and cached market metadata.'
                                  if transport == 'websocket' else
                                  'Act on the newest fill in the first public page before older pages or older rows. Out-of-band buys are decided before a book fetch.')))
        def handle_rows(rows, preserve_order=False, received_at=None):
            fresh = fresh_trades(rows, journal.contains, config, observer_start, preserve_order=preserve_order)
            planned = []
            for key, row in fresh:
                stamp = int(row['timestamp'])
                first = key not in observations
                if first:
                    seen_clock = received_at.get(id(row), time.time()) if received_at else time.time()
                    observations[key] = {'delay':seen_clock-stamp,
                                         'continuous_sample':stamp>=run_started_wall}
                backlog = stamp < run_started_wall
                reason = 'resume_backlog_not_copied' if backlog else source_skip(dict(row, side='BUY'), config, time.time())
                if not reason:
                    try:
                        if outside_copy_band(row, config):
                            reason = 'outside_copy_price_band'
                    except (ValueError, KeyError, TypeError, ArithmeticError):
                        reason = None
                market_future = row.pop('_market_future', None)
                book_future = row.pop('_book_future', None)
                cached_market = row.pop('_cached_market', None)
                if not reason:
                    # A frame-time future is already running for stream trades.
                    # Fall back to a fetch here for the polled path.
                    if book_future is None:
                        book_future = pool.submit(paper_get_json, 'https://clob.polymarket.com/book', {'token_id':row['token_id']})
                    if cached_market is None and market_future is None:
                        market_future = pool.submit(paper_get_json, 'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'], safe=''))
                if first:
                    emit(dict(status='OBSERVED',event_id=key,side=row['side'],slug=row['slug'],
                              outcome=row.get('outcome'),price=row.get('price'),
                              source_transaction=row.get('transaction_hash'),
                              source_timestamp_seconds=row['timestamp'],
                              first_seen_at_utc=datetime.now(timezone.utc).isoformat(),
                              source_to_first_seen_seconds=round(observations[key]['delay'],3),
                              continuous_run_latency_sample=observations[key]['continuous_sample'],
                              seen_before_run_start=not observations[key]['continuous_sample']))
                planned.append((key, row, market_future, book_future, cached_market, reason))
            for key, row, market_future, book_future, cached_market, reason in planned:
                try:
                    if reason:
                        decision = journal.process(key, row, {}, {}, time.time(), skip_reason=reason)
                    else:
                        market = cached_market if cached_market is not None else _ready(market_future)
                        decision = journal.process(key, row, market, _ready(book_future), time.time())
                    note_decision(decision)
                    emit(decision)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc),event_id=key))
                    if not journal.contains(key):
                        try:
                            decision = journal.process(key, row, {}, {}, time.time(), skip_reason='book_unavailable')
                            note_decision(decision)
                            emit(decision)
                        except Exception:
                            pass
        def check_exits():
            for token, position in list(journal.holdings().items()):
                if position['shares'] <= 0:
                    continue
                window = config.get('exit_window_seconds')
                opened = position.get('entry_source_timestamp')
                if window is not None and opened is not None and time.time()-int(opened) > float(window):
                    continue
                try:
                    market_future=pool.submit(paper_get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                    book_future=pool.submit(paper_get_json,'https://clob.polymarket.com/book',{'token_id':token})
                    exited=journal.realize_if_bid_above_cost(_ready(market_future),_ready(book_future),time.time())
                    if exited:
                        emit(exited)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))
        feed = None
        market_cache = None
        try:
            if transport == 'websocket':
                market_cache = MarketCache()
                market_cache.start()
                market_cache.ready.wait(2.5)
                def warm(_index):
                    slug = active_btc_slugs(time.time())[0]
                    try:
                        warmed = paper_get_json('https://gamma-api.polymarket.com/markets/slug/'+urlquote(slug, safe=''))
                        tokens = array((warmed or {}).get('clobTokenIds') or [])
                        if tokens:
                            paper_get_json('https://clob.polymarket.com/book', {'token_id': tokens[0]})
                    except Exception:
                        return None
                for fut in [pool.submit(warm, i) for i in range(8)]:
                    try:
                        _ready(fut, 8)
                    except Exception:
                        pass
                def prepare(row):
                    try:
                        stamp = int(row['timestamp'])
                    except (TypeError, ValueError):
                        return
                    if stamp < run_started_wall:
                        return
                    try:
                        if outside_copy_band(row, config):
                            return
                    except (ValueError, KeyError, TypeError, ArithmeticError):
                        return
                    if source_skip(dict(row, side='BUY'), config, time.time()):
                        return
                    cached = market_cache.get(row.get('slug'))
                    if cached is not None:
                        row['_cached_market'] = cached
                    else:
                        row['_market_future'] = pool.submit(
                            paper_get_json, 'https://gamma-api.polymarket.com/markets/slug/'+urlquote(str(row.get('slug') or ''), safe=''))
                    row['_book_future'] = pool.submit(
                        paper_get_json, 'https://clob.polymarket.com/book', {'token_id': row['token_id']})
                feed = PublicTradeFeed(config['leader_wallet'], prepare=prepare)
                feed.start()
                while time.monotonic()-started < args.duration:
                    try:
                        item = feed.queue.get(timeout=0.15)
                    except queue.Empty:
                        check_exits()
                        close_windows()
                        continue
                    if item[0] == 'error':
                        emit(dict(status='ERROR',message=item[1] if len(item) > 1 else 'trade_feed_error'))
                        continue
                    arrived = [item]
                    while True:
                        try:
                            nxt = feed.queue.get_nowait()
                        except queue.Empty:
                            break
                        if nxt[0] == 'trade':
                            arrived.append(nxt)
                    rows, clocks = [], {}
                    for _, seen_at, row in arrived:
                        clocks[id(row)] = seen_at
                        rows.append(row)
                    # Arrival order is the moment each fill appeared. Do not reorder it.
                    handle_rows(rows, preserve_order=True, received_at=clocks)
                    check_exits()
                    close_windows()
            else:
                while time.monotonic()-started < args.duration:
                    next_request = time.monotonic()+config['poll_seconds']
                    try:
                        end = int(time.time())
                        pages = activity_pages_newest(
                            config['leader_wallet'], max(source_start, end-lookback), end, fetch=paper_get_json)
                        for page in pages:
                            handle_rows(page)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                        next_request=max(next_request,time.monotonic()+2)
                    check_exits()
                    close_windows()
                    remaining=args.duration-(time.monotonic()-started)
                    if remaining > 0:
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
        except KeyboardInterrupt:
            pass
        finally:
            if feed is not None:
                feed.close()
            if market_cache is not None:
                market_cache.close()
        close_windows()
        while closed_windows < planned_windows:
            closed_windows += 1
            snapshot = journal.portfolio()
            snapshot.update(status='WINDOW',window=closed_windows,paper=True,executed=False,
                            window_end_utc=datetime.now(timezone.utc).isoformat())
            emit(snapshot)
        emit(journal.portfolio())
        delays=sorted(v['delay'] for v in observations.values() if v['continuous_sample'] and v['delay']>=0)
        decision_latencies.sort()
        emit(dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),observations=len(observations),
                  windows=closed_windows,
                  continuous_run_latency_samples=len(delays),
                  first_seen_delay_min_seconds=min(delays) if delays else None,
                  first_seen_delay_max_seconds=max(delays) if delays else None,
                  decision_latency_samples=len(decision_latencies),
                  decision_latency_min_seconds=decision_latencies[0] if decision_latencies else None,
                  decision_latency_max_seconds=decision_latencies[-1] if decision_latencies else None,
                  note='Paper decisions use the public book after his fill is visible. No live orders.'))


if __name__=='__main__':
    main()
