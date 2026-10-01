"""Persistent cash-and-inventory paper copy test using public executable depth.

No orders are submitted. Fees use Gamma feeSchedule and the documented formula
https://docs.polymarket.com/trading/fees (verified 2026-09-30). No settlement.
SELL fraction uses source holdings observed during this session only; earlier
leader inventory is unknown and unmatched SELLs are refused.

BUY copies keep his side and market only when the live book can buy the
5-share minimum at his fill price or better. If latency has already moved the
ask above that price, the buy is skipped. Each new leader trade is decided as soon
as the poll that first contains it returns. Nothing waits for a later trade
before that decision. A paper position is sold inside the
same window when the bid is above its average cost and the sale nets a gain.
Open cost stays under max_open_cost_usd, which is below the cash balance, so
one burst cannot spend the whole account. These rules do not guarantee a profit.
"""
import argparse
import http.client
import json
import math
import re
import sqlite3
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
# Faster than config poll_seconds. Idle polls wait this long between request
# starts; a trade already in hand is decided before that wait.
RUNTIME_POLL_SECONDS = 0.05


def same_run_window(source_timestamp, now, run_started):
    """True when both clocks fall in the same 60-second window from run start."""
    if source_timestamp is None:
        return False
    return int(float(source_timestamp) - float(run_started)) // 60 == int(float(now) - float(run_started)) // 60


def _public_read_path(host, path):
    if host == "data-api.polymarket.com":
        return path == "/v2/activity"
    if host == "gamma-api.polymarket.com":
        return path.startswith("/markets/slug/") and not path.endswith("/markets/slug/")
    if host == "clob.polymarket.com":
        return path == "/book"
    return False


class PublicReadClient:
    """Keep-alive GET client for public market data.

    Refuses order, auth, and signing URLs. Several connections per host so a
    burst of book reads can run together. Strategy filters are unchanged.
    """

    def __init__(self, pool_size=4):
        self._pool_size = pool_size
        self._slots = {}
        self._guard = threading.Lock()
        self._rr = 0

    def close(self):
        with self._guard:
            slots = [slot for group in self._slots.values() for slot in group]
            self._slots.clear()
        for slot in slots:
            with slot['lock']:
                conn = slot.get('conn')
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _reserve(self, host):
        with self._guard:
            group = self._slots.setdefault(host, [])
            if len(group) < self._pool_size:
                group.append({'conn': None, 'lock': threading.Lock()})
            self._rr += 1
            order = list(group)
            start = self._rr % len(order)
        for offset in range(len(order)):
            slot = order[(start + offset) % len(order)]
            if slot['lock'].acquire(blocking=False):
                return slot
        slot = order[start]
        slot['lock'].acquire()
        return slot

    def get_json(self, base, params=None):
        parts = urlsplit(base)
        path = parts.path or "/"
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443) or not _public_read_path(parts.hostname, path)):
            raise ValueError("public_read_only")
        query = urlencode(params) if params else parts.query
        target = path + (("?" + query) if query else "")
        slot = self._reserve(parts.hostname)
        try:
            error = None
            for _ in range(2):
                conn = slot['conn']
                if conn is None:
                    conn = http.client.HTTPSConnection(parts.hostname, timeout=5)
                    slot['conn'] = conn
                try:
                    conn.request("GET", target, headers={
                        "User-Agent": "btc-copy-paper-prototype/0.1",
                        "Accept": "application/json",
                        "Connection": "keep-alive",
                    })
                    response = conn.getresponse()
                    body = response.read()
                    status = response.status
                except Exception as exc:
                    error = exc
                    try:
                        conn.close()
                    except Exception:
                        pass
                    slot['conn'] = None
                    continue
                if status != 200:
                    raise ValueError("public_read_http_" + str(status))
                return json.loads(body)
            raise error
        finally:
            slot['lock'].release()


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


FIVE = Decimal('5')


def buy_entry(book, source_price, rate, tick, minimum, budget, open_room, per_buy_room, worse_than_leader=ZERO):
    """Size a BUY at the 5-share minimum, or raise a rule name.

    The fill must be at his price, better, or at most worse_than_leader above it.
    A worse ask means latency already forced a worse price, so the buy is skipped.
    Passing does not guarantee a profit. The copied market and side are his.
    """
    if budget <= 0:
        if open_room <= 0 and per_buy_room > 0:
            raise ValueError('open_risk_cap')
        raise ValueError('five_shares_exceed_per_buy_budget')
    ceiling = source_price + worse_than_leader
    asks = levels(book, 'BUY')
    if not asks:
        raise ValueError('five_shares_not_at_or_better_than_leader_fill')
    if asks[0][0] > ceiling:
        raise ValueError('latency_worse_than_leader_price')
    required = max(minimum, FIVE)
    required = (required / STEP).to_integral_value(rounding=ROUND_CEILING) * STEP
    limit = (ceiling / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    try:
        preview = quote(book, 'BUY', required, rate, limit)
    except ValueError:
        raise ValueError('five_shares_not_at_or_better_than_leader_fill')
    if preview['vwap'] > ceiling:
        raise ValueError('latency_worse_than_leader_price')
    debit = preview['gross'] + preview['fee']
    if debit > budget:
        if open_room < per_buy_room and debit <= per_buy_room:
            raise ValueError('open_risk_cap')
        raise ValueError('five_shares_exceed_per_buy_budget')
    return required, limit


def buy_quantity(book, budget, rate, limit):
    remaining, shares = budget, ZERO
    for price, available in levels(book, 'BUY', limit):
        # Reserve a rounding increment per level; actual charges below use quote.
        spendable = max(ZERO, remaining - Decimal('.00001'))
        take = min(available, spendable / (price + rate * price * (1-price)))
        shares += take
        remaining -= take * (price + rate * price * (1-price)) + Decimal('.00001')
        if take < available:
            break
    return shares.quantize(STEP, rounding=ROUND_DOWN)


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
            decision = dict(status='SKIP',event_id=key,paper=True,executed=False,side=row['side'],slug=row['slug'],
                            outcome=row['outcome'],source_transaction=row.get('transaction_hash'),
                            source_timestamp_seconds=row['timestamp'],
                            source_to_decision_seconds=round(now-int(row['timestamp']),3))
            try:
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
                if row['side']=='BUY':
                    exposure = sum((p['cost'] for p in positions.values()),ZERO)
                    per_buy_room = min(self.cash,D(self.config['max_buy_usd']),
                                       D(self.config['max_outcome_cost_usd'])-position['cost'])
                    open_room = D(self.config['max_open_cost_usd'])-exposure
                    budget = min(per_buy_room, open_room)
                    quantity, limit = buy_entry(
                        book, source_price, rate, tick, minimum, budget, open_room, per_buy_room,
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
                    if debit > budget:
                        raise ValueError('fee_inclusive_budget_exceeded')
                    cash = self.cash-debit
                    position['shares'] += quantity
                    position['cost'] += debit
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
            self.db.execute('INSERT INTO seen VALUES (?,?)',(key,json.dumps(decision)))
            return decision

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
                source_ts = position['row'].get('timestamp')
                decision = dict(status='PAPER_SELL',reason='bid_above_paper_cost',rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),our_price=str(fill['vwap']),
                                realized_pnl_usd=str(net-removed),
                                cash_usd=str(cash),held_shares=str(position['shares']),
                                cost_per_share_usd=str(cost_per))
                if source_ts is not None:
                    decision['source_timestamp_seconds'] = int(source_ts)
                    decision['source_to_decision_seconds'] = round(now-int(source_ts), 3)
                his_price = position['row'].get('price')
                if his_price is not None:
                    decision['his_price'] = str(his_price)
                    decision['cent_difference'] = str((fill['vwap']-D(his_price))*100)
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
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
    raw_config = json.loads(Path(args.config).read_text())
    interval = float(D(raw_config['poll_seconds']))
    if not math.isfinite(interval) or interval < 0.25:
        raise ValueError('paper poll_seconds must be finite and at least 0.25')
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
    journal = PaperJournal(args.db,config)
    stored = journal.db.execute("SELECT value FROM meta WHERE key='observer_start'").fetchone()
    observer_start = int(stored[0]) if stored else int(time.time())
    source_start = observer_start-3600
    client = PublicReadClient()
    runtime_poll = min(interval, RUNTIME_POLL_SECONDS)
    if not stored:
        journal.baseline(activity(config['leader_wallet'],source_start,int(time.time()),client.get_json),observer_start)
    started, run_started_wall, observations = time.monotonic(),time.time(),{}
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=8) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)
        def window_index(moment):
            return int(float(moment)-run_started_wall)//60
        def stamp(record, moment):
            record['run_window_index']=window_index(moment)
            record['paper']=True
            record['executed']=False
            return record
        emit(dict(status='STARTED',continuation=True,starting_cash_usd=str(config['starting_cash_usd']),
                  cash_usd=str(journal.cash),
                  paper=True,executed=False,live_order_sent=False,public_read_only=True,
                  observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  configured_poll_seconds=config['poll_seconds'],runtime_poll_seconds=runtime_poll,
                  poll_seconds=runtime_poll))
        last_window = 0
        emit(dict(status='WINDOW',run_window_index=0,paper=True,executed=False,live_order_sent=False,
                  window_start_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat()))
        def launch_activity():
            def run():
                end=int(time.time())
                return activity(config['leader_wallet'],max(source_start,end-120),end,client.get_json)
            return pool.submit(run), time.monotonic()
        def emit_exit(market, book, decided_at, source_ts):
            exited=journal.realize_if_bid_above_cost(market, book, decided_at)
            if not exited:
                return
            exited['live_order_sent']=False
            exited['same_window_as_entry']=same_run_window(source_ts, decided_at, run_started_wall)
            emit(stamp(exited, decided_at))
        future=None
        request_started=time.monotonic()
        try:
            while time.monotonic()-started < args.duration:
                now_wall = time.time()
                idx = window_index(now_wall)
                while idx > last_window and time.monotonic()-started < args.duration:
                    last_window += 1
                    emit(dict(status='WINDOW',run_window_index=last_window,paper=True,executed=False,
                              live_order_sent=False,
                              window_start_utc=datetime.fromtimestamp(run_started_wall+last_window*60,timezone.utc).isoformat()))
                if future is None:
                    future, request_started = launch_activity()
                try:
                    rows=future.result()
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc),paper=True,executed=False,live_order_sent=False))
                    future=None
                    remaining=args.duration-(time.monotonic()-started)
                    if remaining > 0:
                        time.sleep(min(2, remaining))
                    continue
                future=None
                if time.monotonic()-request_started >= runtime_poll and time.monotonic()-started < args.duration:
                    future, request_started = launch_activity()
                try:
                    pending=[]
                    for key,row in row_keys(rows):
                        if journal.contains(key):
                            continue
                        if (row.get('proxy_wallet','').lower()!=config['leader_wallet'].lower()
                            or row.get('type')!='TRADE' or row.get('is_combo') or row.get('side') not in ('BUY','SELL')
                            or market_timeframe(row.get('slug')) not in config['timeframes_minutes']
                            or int(row['timestamp']) < observer_start):
                            continue
                        pending.append((key,row))
                    # Quotes for every trade already in this response start together.
                    # Decisions still apply in timestamp order so a later trade cannot
                    # change the earlier one. Nothing waits for a trade not yet seen.
                    pending.sort(key=lambda item: int(item[1]['timestamp']))
                    quoted=[]
                    for queue_index,(key,row) in enumerate(pending):
                        seen_at=time.time()
                        if key not in observations:
                            observations[key]={'delay':seen_at-int(row['timestamp']),
                                               'continuous_sample':int(row['timestamp'])>=run_started_wall}
                            emit(stamp(dict(status='OBSERVED',event_id=key,side=row['side'],slug=row['slug'],
                                      outcome=row.get('outcome'),source_price=row.get('price'),
                                      source_transaction=row.get('transaction_hash'),
                                      source_timestamp_seconds=row['timestamp'],
                                      first_seen_at_utc=datetime.fromtimestamp(seen_at,timezone.utc).isoformat(),
                                      source_to_first_seen_seconds=round(observations[key]['delay'],3),
                                      continuous_run_latency_sample=observations[key]['continuous_sample'],
                                      seen_before_run_start=not observations[key]['continuous_sample'],
                                      queue_index=queue_index), seen_at))
                        backlog = int(row['timestamp']) < run_started_wall
                        reason=('resume_backlog_not_copied' if backlog else
                                source_skip(dict(row,side='BUY'),config,time.time()))
                        if reason:
                            quoted.append((queue_index,key,row,None,None,reason))
                        else:
                            market_future=pool.submit(client.get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
                            book_future=pool.submit(client.get_json,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
                            quoted.append((queue_index,key,row,market_future,book_future,None))
                    for queue_index,key,row,market_future,book_future,reason in quoted:
                        try:
                            if reason:
                                market=book={}
                            else:
                                market,book=market_future.result(),book_future.result()
                            decided_at=time.time()
                            decision=journal.process(key,row,market,book,decided_at,
                                                     skip_reason='resume_backlog_not_copied' if reason=='resume_backlog_not_copied' else None)
                            decision['queue_index']=queue_index
                            decision['live_order_sent']=False
                            emit(stamp(decision, decided_at))
                            if decision.get('status')=='PAPER_BUY':
                                emit_exit(market, book, time.time(), row.get('timestamp'))
                        except Exception as exc:
                            emit(dict(status='ERROR',message=str(exc),event_id=key,paper=True,executed=False,live_order_sent=False))
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc),paper=True,executed=False,live_order_sent=False))
                for token,position in list(journal.holdings().items()):
                    if position['shares'] <= 0:
                        continue
                    source_ts = position['row'].get('timestamp')
                    try:
                        market_future=pool.submit(client.get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(client.get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        decided_at=time.time()
                        emit_exit(market_future.result(), book_future.result(), decided_at, source_ts)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc),paper=True,executed=False,live_order_sent=False))
                if future is None and time.monotonic()-started < args.duration:
                    remaining=args.duration-(time.monotonic()-started)
                    wait=runtime_poll-(time.monotonic()-request_started)
                    if remaining > 0 and wait > 0:
                        time.sleep(min(wait, remaining))
                    if time.monotonic()-started < args.duration:
                        future, request_started = launch_activity()
        except KeyboardInterrupt:
            pass
        portfolio=journal.portfolio(client.get_json)
        portfolio['live_order_sent']=False
        emit(portfolio)
        delays=sorted(v['delay'] for v in observations.values() if v['continuous_sample'] and v['delay']>=0)
        emit(dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),observations=len(observations),
                  continuous_run_latency_samples=len(delays),
                  first_seen_delay_min_seconds=min(delays) if delays else None,
                  first_seen_delay_max_seconds=max(delays) if delays else None,
                  live_order_sent=False,
                  note='Snapshot depth simulations; no live fills, settlement or guaranteed execution latency.'))
    client.close()


if __name__=='__main__':
    main()
