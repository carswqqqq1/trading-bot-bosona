"""Persistent cash-and-inventory paper copy test using public executable depth.

No orders are submitted. His public trades are taken from the activity stream
and paper-copied on that same side and market as soon as the trade arrives.
If the stream drops, the public activity API is read again immediately, with
no idle poll. Fees use Gamma feeSchedule and the documented formula
https://docs.polymarket.com/trading/fees (verified 2026-09-30). No settlement.
SELL fraction uses source holdings observed during this session only; earlier
leader inventory is unknown and unmatched SELLs are refused.

BUY copies keep his side and market only when the live book can buy the
5-share minimum at his fill price or better. If latency has already moved the
ask above that price, the buy is skipped. His public history does not support
a skip at 0.85: those prices are a normal part of his buys, and the rows have
no dollar target to compare with an $80 band. There is no chase-after-a-sell
skip because those rows are buys, redeems, and merges, not sells. Markets
outside BTC 5-minute, 15-minute, and hourly windows are skipped. A paper
position is sold inside the same window when the bid is above its average
cost and the sale nets a gain.
Open cost stays under max_open_cost_usd, which is below the cash balance, so
one burst cannot spend the whole account. These rules do not guarantee a profit.
"""
import argparse
import json
import math
import queue
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from urllib.parse import quote as urlquote

from bot import activity, array, decimal as D, get_json, market_timeframe, row_keys, source_skip, validate
from feed import ActivityWatch, BookPrefetcher, JsonClient, QuoteCache, fill_key

ZERO = Decimal(0)
STEP = Decimal('0.01')


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
                decision = dict(status='PAPER_SELL',reason='bid_above_paper_cost',rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),realized_pnl_usd=str(net-removed),
                                cash_usd=str(cash),held_shares=str(position['shares']),
                                cost_per_share_usd=str(cost_per))
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
    parser.add_argument('--until-cash',type=float,default=None,
                        help='Stop the paper run when cash reaches this many dollars')
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error('duration must be positive and finite')
    if args.until_cash is not None and (not math.isfinite(args.until_cash) or args.until_cash <= 0):
        parser.error('--until-cash must be positive and finite')
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
    client = JsonClient()
    stored = journal.db.execute("SELECT value FROM meta WHERE key='observer_start'").fetchone()
    run_started_wall = time.time()
    observer_start = int(stored[0]) if stored else int(run_started_wall)
    source_start = observer_start-3600
    quotes = QuoteCache()
    prefetcher = BookPrefetcher(JsonClient(), quotes)
    seen_fills=set()
    prior=[json.loads(item[0]) for item in journal.db.execute('SELECT payload FROM seen')]
    sample_start_cash=journal.cash
    sample_start_realized=sum((D(item.get('realized_pnl_usd','0')) for item in prior
                               if item.get('status') in ('PAPER_BUY','PAPER_SELL')), ZERO)
    started, observations, pending, attempts = time.monotonic(), {}, {}, {}
    stopped_for_cash=False
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=4) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)
        def warm():
            now=int(time.time())
            slug='btc-updown-5m-'+str(now-(now%300))
            try:
                market=client('https://gamma-api.polymarket.com/markets/slug/'+urlquote(slug,safe=''))
                tokens=array(market['clobTokenIds']) if market.get('clobTokenIds') else []
                for token in tokens:
                    client('https://clob.polymarket.com/book',{'token_id':token})
            except Exception:
                pass
        def book_timestamp_fresh(book, now):
            try:
                age=float(now)*1000-float(book['timestamp'])
            except (KeyError, TypeError, ValueError):
                return False
            return -1000<=age<=float(config['max_book_age_seconds'])*1000
        def on_trade(row):
            hit=quotes.take(str(row['token_id']), row.get('slug'), time.time(), 1.0)
            if hit and book_timestamp_fresh(hit[1], time.time()):
                return ('cache', hit)
            market_future=pool.submit(client,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
            book_future=pool.submit(client,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
            return ('fetch', market_future, book_future)
        watch=ActivityWatch(config['leader_wallet'], on_trade=on_trade)
        watch.start()
        prefetcher.start()
        if not stored:
            older=[row for row in activity(config['leader_wallet'],source_start,int(time.time()),client)
                   if int(row['timestamp'])<run_started_wall]
            journal.baseline(older,observer_start)
            seen_fills.update(fill_key(row) for row in older)
        for job in (pool.submit(warm),pool.submit(warm)):
            job.result()
        connected=watch.connected.wait(2)
        emit(dict(status='STARTED',starting_cash_usd=str(config['starting_cash_usd']),cash_usd=str(journal.cash),
                  sample_start_cash_usd=str(sample_start_cash),
                  paper=True,executed=False,observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  idle_poll_seconds=0,watch='wss://ws-live-data.polymarket.com activity/trades',
                  activity_stream_connected=connected,until_cash_usd=args.until_cash))
        def eligible(row):
            return (row.get('proxy_wallet','').lower()==config['leader_wallet'].lower()
                    and row.get('type')=='TRADE' and not row.get('is_combo') and row.get('side') in ('BUY','SELL')
                    and market_timeframe(row.get('slug')) in config['timeframes_minutes']
                    and int(row['timestamp'])>=observer_start)
        def consider(row, arrived, transport, prepared=None):
            if not eligible(row):
                return
            identity=fill_key(row)
            if identity in seen_fills or identity in pending:
                return
            pending[identity]=(row,arrived,transport,prepared)
        def live_quote(row):
            fetch_started=time.perf_counter()
            market_future=pool.submit(client,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
            book_future=pool.submit(client,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
            return market_future.result(), book_future.result(), time.perf_counter()-fetch_started, 0.0, 'live'
        def resolve_quote(row, prepared):
            if prepared and prepared[0]=='cache':
                market, book, age = prepared[1]
                if book_timestamp_fresh(book, time.time()):
                    return market, book, 0.0, age, 'prefetch'
            if prepared and prepared[0]=='fetch':
                fetch_started=time.perf_counter()
                return prepared[1].result(), prepared[2].result(), time.perf_counter()-fetch_started, 0.0, 'live'
            return live_quote(row)
        def copy_now(row, arrived, transport, prepared):
            identity=fill_key(row)
            key=next(item[0] for item in row_keys([row]))
            waited=max(0, time.time()-arrived)
            if identity not in observations:
                delay=arrived-int(row['timestamp'])
                observations[identity]=dict(delay=delay,continuous_sample=int(row['timestamp'])>=run_started_wall,
                                            transport=transport,decision=None,fetch=None,quote=None)
                emit(dict(status='OBSERVED',event_id=key,side=row['side'],slug=row['slug'],outcome=row.get('outcome'),
                          source_transaction=row.get('transaction_hash'),source_timestamp_seconds=row['timestamp'],
                          first_seen_at_utc=datetime.fromtimestamp(arrived,timezone.utc).isoformat(),
                          source_to_first_seen_seconds=round(delay,3),transport=transport,
                          continuous_run_latency_sample=observations[identity]['continuous_sample'],
                          seen_before_run_start=not observations[identity]['continuous_sample']))
            backlog=int(row['timestamp'])<run_started_wall
            reason='resume_backlog_not_copied' if backlog else source_skip(dict(row,side='BUY'),config,time.time())
            market=book={}
            fetch_seconds=quote_age=None
            quote_source=None
            if not reason:
                market, book, fetch_seconds, quote_age, quote_source = resolve_quote(row, prepared)
                observations[identity]['fetch']=fetch_seconds
                observations[identity]['quote']=quote_source
            decision=journal.process(key,row,market,book,time.time(),
                                     skip_reason='resume_backlog_not_copied' if backlog else None)
            decision['transport']=transport
            decision['queue_wait_seconds']=round(waited,3)
            if fetch_seconds is not None:
                decision['book_fetch_seconds']=round(fetch_seconds,3)
                decision['quote_age_seconds']=round(quote_age,3)
                decision['quote_source']=quote_source
            if decision.get('source_to_decision_seconds') is not None:
                observations[identity]['decision']=decision['source_to_decision_seconds']
            emit(decision)
        def flush():
            for identity,(row,arrived,transport,prepared) in list(pending.items()):
                try:
                    copy_now(row,arrived,transport,prepared)
                except Exception as exc:
                    attempts[identity]=attempts.get(identity,0)+1
                    emit(dict(status='ERROR',message=str(exc),source_transaction=row.get('transaction_hash')))
                    if attempts[identity]<4 and time.time()-int(row['timestamp'])<=config['max_signal_age_seconds']:
                        continue
                pending.pop(identity,None)
                seen_fills.add(identity)
        def exit_scan():
            for token,position in list(journal.holdings().items()):
                if position['shares']<=0:
                    continue
                try:
                    hit=quotes.take(token, position['row'].get('slug'), time.time(), 1.0)
                    if hit and book_timestamp_fresh(hit[1], time.time()):
                        market, book = hit[0], hit[1]
                    else:
                        market_future=pool.submit(client,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(client,'https://clob.polymarket.com/book',{'token_id':token})
                        market, book = market_future.result(), book_future.result()
                    exited=journal.realize_if_bid_above_cost(market,book,time.time())
                    if exited:
                        emit(exited)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))
        try:
            next_exit=time.monotonic()
            was_connected=connected
            while time.monotonic()-started<args.duration:
                if watch.connected.is_set()!=was_connected:
                    was_connected=watch.connected.is_set()
                    emit(dict(status='WATCH',activity_stream_connected=was_connected,
                              error=watch.errors[-1] if watch.errors and not was_connected else None))
                remaining=args.duration-(time.monotonic()-started)
                if watch.connected.is_set():
                    try:
                        pending_item=watch.queue.get(timeout=min(0.25,max(0,remaining)))
                    except queue.Empty:
                        pending_item=None
                    batch=[pending_item] if pending_item else []
                    while True:
                        try:
                            batch.append(watch.queue.get_nowait())
                        except queue.Empty:
                            break
                    for arrived,row,transport,prepared in batch:
                        consider(row,arrived,transport,prepared)
                else:
                    try:
                        end=int(time.time())
                        rows=activity(config['leader_wallet'],max(source_start,end-120),end,client)
                        seen_at=time.time()
                        for _,row in row_keys(rows):
                            consider(row,seen_at,'activity_rest',None)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                        time.sleep(min(0.5,max(0,args.duration-(time.monotonic()-started))))
                flush()
                if args.until_cash is not None and journal.cash>=D(str(args.until_cash)):
                    stopped_for_cash=True
                    emit(dict(status='CASH_TARGET',cash_usd=str(journal.cash),target_usd=str(args.until_cash)))
                    break
                if not pending and watch.queue.empty() and time.monotonic()>=next_exit:
                    exit_scan()
                    next_exit=time.monotonic()+0.25
                    if args.until_cash is not None and journal.cash>=D(str(args.until_cash)):
                        stopped_for_cash=True
                        emit(dict(status='CASH_TARGET',cash_usd=str(journal.cash),target_usd=str(args.until_cash)))
                        break
        except KeyboardInterrupt:
            pass
        finally:
            watch.close()
            prefetcher.close()
        report=journal.portfolio(client)
        emit(report)
        sample_realized=D(report.get('realized_pnl_usd','0'))-sample_start_realized
        fresh=[item for item in observations.values() if item['continuous_sample'] and item['delay']>=0]
        delays=sorted(item['delay'] for item in fresh)
        decisions=sorted(item['decision'] for item in fresh if item['decision'] is not None)
        fetches=sorted(item['fetch'] for item in fresh if item['fetch'] is not None)
        def median(values):
            if not values:
                return None
            return (values[(len(values)-1)//2]+values[len(values)//2])/2
        emit(dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),observations=len(observations),
                  continuous_run_latency_samples=len(delays),
                  first_seen_delay_min_seconds=delays[0] if delays else None,
                  first_seen_delay_median_seconds=median(delays),
                  first_seen_delay_p95_seconds=delays[math.ceil(len(delays)*0.95)-1] if delays else None,
                  first_seen_delay_max_seconds=delays[-1] if delays else None,
                  decision_delay_min_seconds=decisions[0] if decisions else None,
                  decision_delay_median_seconds=median(decisions),
                  decision_delay_p95_seconds=decisions[math.ceil(len(decisions)*0.95)-1] if decisions else None,
                  decision_delay_max_seconds=decisions[-1] if decisions else None,
                  book_fetch_median_seconds=median(fetches),
                  book_fetch_max_seconds=fetches[-1] if fetches else None,
                  websocket_samples=sum(item['transport']=='activity_websocket' for item in fresh),
                  rest_fallback_samples=sum(item['transport']=='activity_rest' for item in fresh),
                  prefetch_quotes=sum(item.get('quote')=='prefetch' for item in fresh),
                  live_quotes=sum(item.get('quote')=='live' for item in fresh),
                  sample_start_cash_usd=str(sample_start_cash),
                  sample_realized_pnl_usd=str(sample_realized),
                  ending_cash_usd=str(journal.cash),
                  stopped_because='cash_target' if stopped_for_cash else 'duration',
                  idle_poll_seconds=0,
                  note='Public activity-stream detection and a paper book quote. Source timestamps are 1-second precision. No live fills and no guaranteed profit.'))


if __name__=='__main__':
    main()
