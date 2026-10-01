"""Persistent cash-and-inventory paper copy test using public executable depth.

No orders are submitted. Fees use Gamma feeSchedule and the documented formula
https://docs.polymarket.com/trading/fees (verified 2026-09-30). No settlement.
SELL fraction uses source holdings observed during this session only; earlier
leader inventory is unknown and unmatched SELLs are refused.

BUY copies keep his side and market only when the live book can buy the
5-share minimum at his fill price or better. If latency has already moved the
ask above that price, the buy is skipped. A paper position is sold inside the
same window when the bid is above its average cost and the sale nets a gain.
Open cost stays under max_open_cost_usd, which is below the cash balance, so
one burst cannot spend the whole account. These rules do not guarantee a profit.

strategy "paper_c" is a separate filter. A sell is copied only when it
closes an existing paper position above paper cost. With no matching position
the sell is skipped when it appears. copy_buys_at_or_better copies his buy on
the same side and market, for his full size, only when that full size fills
at his price or better. A better full-size price is a copy. A 5-share quote
is not that price. The market minimum is still 5 shares. If that size costs
more than the cash on hand, the buy is skipped and is not scaled down. The
copy is a taker. On a new buy the fee is taken in shares, so the held count
is his size minus fee divided by price. On a sell the fee comes out of the
USDC proceeds. A resolution is not a fill and has no fee. Makers pay nothing.
A copied buy is one of his printed trades: same side, same market, his exact
share count, and only when that full size is on the book at his price or
better. Under 5 shares, a missing size, or a cost above the cash is a skip.
Nothing is scaled down and no fill is invented. Once that position is open,
paper accounting sells the size posted at the best bid when that bid is no
longer above paper cost. A bid that is still above cost is held. Every fill
logs his transaction hash and the seconds from his fill to the decision.
The clock is seconds from the book's start until cash reaches the goal.
The paper48 path is unchanged.
"""
import argparse
import json
import math
import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from urllib.parse import quote as urlquote

from bot import (activity, array, decimal as D, get_json, market_timeframe, public_client,
                 RateLimited, row_keys, source_skip, validate)

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


def taker_fee_usdc(shares, rate, price):
    """Taker fee in USDC. Rounded to 5 decimals; anything under 0.00001 is 0."""
    raw = shares * rate * price * (1 - price)
    if raw < Decimal('0.00001'):
        return ZERO
    fee = raw.quantize(Decimal('0.00001'), rounding=ROUND_HALF_UP)
    if fee < Decimal('0.00001'):
        return ZERO
    return fee


def taker_fee_rate(market, slug=None):
    """Taker rate for a new paper fill. Makers pay nothing and there is no rebate.

    Bitcoin up-or-down markets are crypto, so the rate is 0.07 unless the
    market's own fee fields name a different taker rate. Fees off, or a
    geopolitics market, charge 0.
    """
    market = market if isinstance(market, dict) else {}
    if market.get('feesEnabled') is False:
        return ZERO
    labels = [str(market.get(key) or '') for key in ('category', 'feeType', 'slug')]
    tags = market.get('tags') or []
    if isinstance(tags, list):
        labels.extend(str(tag.get('label') if isinstance(tag, dict) else tag) for tag in tags)
    if slug:
        labels.append(str(slug))
    if 'geopolitic' in ' '.join(labels).lower():
        return ZERO
    schedule = market.get('feeSchedule') or {}
    if schedule.get('rate') is not None:
        rate = D(schedule['rate'])
        if not 0 <= rate <= 1:
            raise ValueError('invalid_fee_rate')
        return rate
    name = str(slug or market.get('slug') or '')
    if name.startswith('btc-updown-') or name.startswith('bitcoin-up-or-down-'):
        return Decimal('0.07')
    return fee_rate(market)


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
    remaining, gross, fees, share_fee = quantity, ZERO, ZERO, ZERO
    for price, available in levels(book, side, limit):
        take = min(remaining, available)
        if take <= 0:
            continue
        gross += take * price
        level_fee = taker_fee_usdc(take, rate, price)
        fees += level_fee
        share_fee += level_fee / price
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise ValueError('insufficient_depth')
    return dict(shares=quantity, gross=gross, fee=fees, share_fee=share_fee, vwap=gross/quantity)


def paper_goal(config):
    """Paper equity target. This run's target is $75 from a $37.40 start."""
    return D(config.get('goal_usd') or '75')


def seconds_from_start_to_goal(started_at, now, equity, goal):
    """Seconds from the book's start until equity reaches the goal, or None."""
    if started_at is None or now is None or equity is None or equity < goal:
        return None
    return round(float(now) - float(started_at), 3)


def book_started_at(journal, now):
    """Persist the book's start once. Restarts keep the same clock."""
    row = journal.db.execute("SELECT value FROM meta WHERE key='book_started_at'").fetchone()
    if row:
        return float(row[0])
    stored = journal.db.execute("SELECT value FROM meta WHERE key='observer_start'").fetchone()
    started = float(stored[0]) if stored else float(now)
    with journal.db:
        journal.db.execute("INSERT OR IGNORE INTO meta VALUES ('book_started_at', ?)", (str(started),))
    return started


def quoted_equity(port):
    """Cash plus a full liquidation quote. An unquoted position is not equity."""
    if not port or port.get('unresolved_positions'):
        return None
    raw = port.get('equity_at_liquidation_quote_usd')
    if raw is None:
        return None
    return D(raw)


def mark_goal_reached(journal, now, equity, goal):
    """Seconds from the book start to the first time quoted equity hits the goal.

    The first crossing stays stored. A later check above the goal reports that
    same clock. A check below the goal reports nothing and leaves the stamp.
    """
    started = book_started_at(journal, now)
    row = journal.db.execute("SELECT value FROM meta WHERE key='goal_reached_at'").fetchone()
    reached_at = float(row[0]) if row else None
    if reached_at is None and equity is not None and equity >= goal:
        reached_at = float(now)
        with journal.db:
            journal.db.execute("INSERT OR IGNORE INTO meta VALUES ('goal_reached_at', ?)", (str(reached_at),))
        stored = journal.db.execute("SELECT value FROM meta WHERE key='goal_reached_at'").fetchone()
        reached_at = float(stored[0]) if stored else reached_at
    if reached_at is None or equity is None or equity < goal:
        return None
    return seconds_from_start_to_goal(started, reached_at, equity, goal)


def best_bid_sell_block(book, shares, cost, rate):
    """Reason to skip, or None when the best bid can sell at least half above cost.

    Only size posted at the best bid price counts. The next price is not used,
    and a missing bid is not replaced with a made-up fill. Half the position
    is enough. The sale of that half must still be strictly above paper cost.
    """
    info = dict(position_shares=str(shares), paper_cost_usd=str(cost))
    if shares <= 0:
        return 'best_bid_cannot_sell_half_position', info
    half = shares * Decimal('0.5')
    info['required_bid_shares'] = str(half)
    if not isinstance(book, dict) or not book.get('bids'):
        return 'no_bid', info
    bids = levels(book, 'SELL')
    if not bids:
        return 'no_bid', info
    price = bids[0][0]
    same = [(level_price, level_size) for level_price, level_size in bids if level_price == price]
    size = sum((level_size for _, level_size in same), ZERO)
    info.update(best_bid_price=str(price), best_bid_size=str(size))
    if size <= 0:
        return 'no_bid', info
    if size < half:
        return 'best_bid_cannot_sell_half_position', info
    only = {'bids': [{'price': str(level_price), 'size': str(level_size)} for level_price, level_size in same],
            'asks': []}
    try:
        fill = quote(only, 'SELL', half, rate)
    except ValueError:
        return 'best_bid_cannot_sell_half_position', info
    portion = cost * half / shares
    net = fill['gross'] - fill['fee']
    info['best_bid_net_usd'] = str(net)
    info['half_cost_usd'] = str(portion)
    if fill['vwap'] <= cost / shares or net <= portion:
        return 'best_bid_not_strictly_above_paper_cost', info
    return None, info


def leader_trade_row(payload, wallet):
    """Map one public activity-stream trade into the paper row shape.

    Another wallet is ignored. The row is still only a signal. The live book
    decides whether it can be copied.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get('proxyWallet') or '').lower() != str(wallet).lower():
        return None
    side = str(payload.get('side') or '').upper()
    if side not in ('BUY', 'SELL'):
        return None
    slug = payload.get('slug') or payload.get('eventSlug')
    if not slug:
        return None
    try:
        timestamp = int(payload.get('timestamp'))
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        size = D(payload.get('size'))
        price = D(payload.get('price'))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if size <= 0 or not 0 < price < 1:
        return None
    return dict(proxy_wallet=str(payload.get('proxyWallet')).lower(),
                transaction_hash=payload.get('transactionHash'),
                condition_id=payload.get('conditionId') or '',
                token_id=str(payload.get('asset')),
                timestamp=timestamp, side=side, size=str(size), price=str(price),
                usdc_size=str(size * price), type='TRADE', slug=slug,
                outcome=payload.get('outcome'), is_combo=False)


def trade_identity(row):
    """Same trade from the poll and the public stream shares one id."""
    size = D(row['size']).quantize(Decimal('0.0001'))
    price = D(row['price']).quantize(Decimal('0.0001'))
    return f"tx:{str(row.get('transaction_hash') or '').lower()}:{row.get('token_id')}:{row.get('side')}:{size}:{price}"


def median_latency(values):
    """Median of a latency list, or None when there is no fill."""
    numbers = sorted(value for value in values if isinstance(value, (int, float)))
    if not numbers:
        return None
    mid = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[mid]
    return round((numbers[mid - 1] + numbers[mid]) / 2, 3)


FIVE = Decimal('5')


def public_resolution_price(market, outcome, token):
    """Payout per share from a resolved Gamma market, or (None, reason).

    The price is the published outcomePrices entry for this outcome. A last
    trade is not a resolution price.
    """
    if not isinstance(market, dict):
        return None, 'resolution_unavailable'
    if market.get('umaResolutionStatus') != 'resolved' or market.get('closed') is not True:
        return None, 'not_resolved'
    try:
        outcomes, prices = array(market.get('outcomes')), array(market.get('outcomePrices'))
        tokens = array(market.get('clobTokenIds'))
    except (ValueError, TypeError):
        return None, 'resolution_price_missing'
    if not isinstance(outcomes, list) or not isinstance(prices, list) or len(outcomes) != len(prices):
        return None, 'resolution_price_missing'
    if outcome not in outcomes:
        return None, 'resolution_outcome_missing'
    index = outcomes.index(outcome)
    if isinstance(tokens, list) and len(tokens) == len(outcomes) and str(tokens[index]) != str(token):
        return None, 'resolution_token_mismatch'
    try:
        price = D(prices[index])
    except (ValueError, TypeError, ArithmeticError):
        return None, 'resolution_price_missing'
    if price < 0 or price > 1:
        return None, 'resolution_price_missing'
    return price, None


def steadily_losing(samples):
    """True when cash plus open value drifts down across the sample.

    One down step is not a steady loss. A flat or rising sample is not a loss.
    """
    if len(samples) < 3:
        return False
    if any(later > earlier for earlier, later in zip(samples, samples[1:])):
        return False
    downs = sum(1 for earlier, later in zip(samples, samples[1:]) if later < earlier)
    return downs >= 2 and samples[-1] < samples[0]


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
            try:
                identity = trade_identity(row)
            except (ArithmeticError, TypeError, ValueError, KeyError):
                identity = None
            if identity and self.contains(identity):
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
            latency = round(now-int(row['timestamp']),3)
            decision = dict(status='SKIP',event_id=key,paper=True,executed=False,side=row['side'],slug=row['slug'],
                            outcome=row['outcome'],source_transaction=row.get('transaction_hash'),
                            source_timestamp_seconds=row['timestamp'],source_size=str(row.get('size')),
                            source_to_decision_seconds=latency,decision_latency_seconds=latency,
                            decision_at_utc=datetime.fromtimestamp(now,timezone.utc).isoformat())
            try:
                priced = D(row['price'])
                if 0 < priced < 1:
                    decision['his_price'] = str(priced)
                    decision['source_price'] = str(priced)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                pass
            try:
                if skip_reason:
                    raise ValueError(skip_reason)
                # A sell with nothing open is skipped before any book read.
                # Buys stay closed unless the one rule change, copy_buys_at_or_better, is on.
                if self.config.get('strategy')=='paper_c' and row['side']=='SELL' and position['shares']<=0:
                    raise ValueError('no_matching_paper_position')
                if (self.config.get('strategy')=='paper_c' and row['side']=='BUY'
                        and not self.config.get('copy_buys_at_or_better')):
                    raise ValueError('paper_c_no_new_buys')
                reason = market_check(row,market,book,self.config,now)
                if reason:
                    raise ValueError(reason)
                rate = (taker_fee_rate(market, row.get('slug'))
                        if self.config.get('strategy')=='paper_c' else fee_rate(market))
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
                exact_buy = False
                if self.config.get('strategy')=='paper_c' and row['side']=='SELL':
                    # His sell, his market, and only a close that nets a gain above paper cost.
                    quantity = position['shares'].quantize(STEP,rounding=ROUND_DOWN)
                    cost_per = position['cost']/position['shares']
                    limit = cost_per
                    deny = 'sell_not_above_paper_cost'
                    if self.config.get('sell_must_match_his_price'):
                        limit = max(limit, source_price)
                        deny = 'sell_worse_than_his_price'
                    if quantity < minimum or quantity <= 0:
                        raise ValueError('below_market_minimum_or_budget_cap')
                    try:
                        fill = quote(book,'SELL',quantity,rate,limit)
                    except ValueError:
                        raise ValueError(deny)
                    removed_cost = position['cost']*quantity/position['shares']
                    net = fill['gross']-fill['fee']
                    if net <= removed_cost:
                        raise ValueError('sell_not_above_paper_cost')
                    if self.config.get('sell_must_match_his_price') and fill['vwap'] < source_price:
                        raise ValueError('sell_worse_than_his_price')
                    cash = self.cash+net
                    position['cost'] -= removed_cost
                    position['shares'] -= quantity
                    if position['shares']==0:
                        position['cost'] = ZERO
                    decision['realized_pnl_usd'] = str(net-removed_cost)
                elif row['side']=='BUY' and self.config.get('strategy')=='paper_c' and self.config.get('copy_buys_at_or_better'):
                    # His printed size, same side and market. The book must hold
                    # that full size at his price or better. A smaller size is
                    # not a copy. Cash that cannot cover it skips the buy.
                    quantity = source_shares
                    if quantity < max(minimum, FIVE):
                        raise ValueError('below_market_minimum')
                    try:
                        fill = quote(book,'BUY',quantity,rate)
                    except ValueError:
                        raise ValueError('his_size_not_on_the_book')
                    decision['simulated_vwap'] = str(fill['vwap'])
                    if fill['vwap'] > source_price:
                        raise ValueError('latency_worse_than_leader_price')
                    received = quantity - fill['share_fee']
                    if received <= 0:
                        raise ValueError('taker_fee_consumed_the_shares')
                    debit = fill['gross']
                    if debit > self.cash:
                        raise ValueError('his_size_exceeds_cash')
                    cash = self.cash-debit
                    opening = position['shares'] == 0
                    position['shares'] += received
                    position['cost'] += debit
                    fill = dict(fill, shares=received)
                    decision['fee_collected_in'] = 'shares'
                    decision['share_fee'] = str(fill['share_fee'])
                    decision['leader_transaction_hash'] = row.get('transaction_hash')
                    if opening:
                        position['opened_at'] = now
                        position['opened_source_timestamp'] = row['timestamp']
                        position['opened_transaction'] = row.get('transaction_hash')
                    exact_buy = True
                elif row['side']=='BUY':
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
                if not (self.config.get('strategy')=='paper_c' and row['side']=='SELL') and not exact_buy:
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
            exact_size_buy = (self.config.get('strategy')=='paper_c'
                              and self.config.get('copy_buys_at_or_better') and row['side']=='BUY')
            if our_price is None and not exact_size_buy:
                our_price = (decision.get('minimum_size_price_comparison') or {}).get('snapshot_vwap')
            if our_price is not None and decision.get('source_price') is not None:
                decision['our_price'] = str(our_price)
                decision['his_price'] = str(decision['source_price'])
                # Positive means our price is above his fill. Same side and market.
                decision['cent_difference'] = str((D(our_price)-D(decision['source_price']))*100)
                decision['cent_gap'] = decision['cent_difference']
            elif decision.get('status')=='SKIP':
                decision['our_price'] = None
                decision['cent_gap'] = None
            if self.config.get('strategy')=='paper_c' and decision.get('status')=='SKIP' and decision.get('our_price') is None:
                decision['price_note'] = 'No paper fill, so there is no our price and no cent gap.'
            if decision.get('reason')=='no_bid':
                decision['our_price'] = None
                decision['cent_gap'] = None
                decision['price_note'] = 'No bid, so no fill is invented.'
            held_after = sum((p['shares'] for tok,p in positions.items() if tok!=token),ZERO)+position['shares']
            if held_after==0:
                decision['unrealized_pnl_usd'] = '0'
            if decision['status']=='SKIP' and 'realized_pnl_usd' not in decision:
                decision['realized_pnl_usd'] = '0'
            if decision.get('status') in ('PAPER_BUY','PAPER_SELL'):
                decision['leader_transaction_hash'] = decision.get('leader_transaction_hash') or row.get('transaction_hash')
                decision['decision_latency_seconds'] = latency
            position['row'] = row
            payload = json.dumps(position,default=str)
            self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',(token,payload))
            self.db.execute('INSERT INTO seen VALUES (?,?)',(key,json.dumps(decision)))
            if identity:
                self.db.execute('INSERT OR IGNORE INTO seen VALUES (?,?)',
                                (identity, json.dumps(dict(status='TRADE_ALIAS', paper=True, executed=False))))
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

    def realize_same_minute_if_bid_above_cost(self, market, book, now):
        """Paper-account a sell once the best bid is no longer above cost.

        This is not one of his prints. While the best bid is still above paper
        cost the position is held. A missing bid is not a fill, and size that
        is not posted at the best bid is not invented.
        """
        if not (self.config.get('strategy')=='paper_c' and self.config.get('sell_same_minute_if_bid_above_cost')):
            return None
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
                rate = taker_fee_rate(market, position['row'].get('slug'))
                minimum = D(book['min_order_size'])
                if minimum <= 0:
                    return None
                cost_per = position['cost']/position['shares']
                bids = levels(book, 'SELL')
                if not bids:
                    return None
                price = bids[0][0]
                if price > cost_per:
                    return None
                size = sum((level_size for level_price, level_size in bids if level_price == price), ZERO)
                quantity = min(position['shares'], size).quantize(STEP, rounding=ROUND_DOWN)
                if quantity < minimum or quantity <= 0:
                    return None
                only = {'bids': [{'price': str(price), 'size': str(size)}], 'asks': []}
                try:
                    fill = quote(only, 'SELL', quantity, rate)
                except ValueError:
                    return None
                removed = position['cost'] * quantity / position['shares']
                net = fill['gross']-fill['fee']
                reason = 'bid_no_longer_above_paper_cost'
                cash = self.cash+net
                position['shares'] -= quantity
                position['cost'] -= removed
                if position['shares'] <= 0:
                    position['shares'] = ZERO
                    position['cost'] = ZERO
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                source_ts = position.get('opened_source_timestamp', position['row'].get('timestamp'))
                latency = None if source_ts is None else round(now-int(source_ts), 3)
                decision = dict(status='PAPER_SELL',reason=reason,rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),our_price=str(fill['vwap']),his_price=None,cent_gap=None,
                                realized_pnl_usd=str(net-removed),cash_usd=str(cash),
                                held_shares=str(position['shares']),
                                cost_per_share_usd=str(cost_per),cost_usd=str(removed),
                                source_timestamp_seconds=source_ts,decision_latency_seconds=latency,
                                leader_transaction_hash=position.get('opened_transaction') or position['row'].get('transaction_hash'),
                                source_transaction=position.get('opened_transaction') or position['row'].get('transaction_hash'),
                                latency_note=('Held while the best bid is above paper cost. '
                                              'Once that bid is no longer above cost, the size posted there is sold. '
                                              'This sell is paper accounting, not one of his prints. '
                                              'The hash is the printed buy that opened the position. '
                                              'Latency is his opening fill to this sell.'))
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
                held_after = sum((p['shares'] for p in self.holdings().values()), ZERO)
                goal = paper_goal(self.config)
                decision['goal_usd'] = str(goal)
                decision['cash_reached_75'] = cash >= goal
                if held_after == 0:
                    decision['unrealized_pnl_usd'] = '0'
                    decision['equity_usd'] = str(cash)
                    decision['equity_reached_goal'] = cash >= goal
                if cash >= goal:
                    decision['seconds_from_start_to_goal'] = mark_goal_reached(self, now, cash, goal)
                self.db.execute('INSERT INTO seen VALUES (?,?)',
                                (f"bid-exit:{token}:{quantity}:{now}",json.dumps(decision)))
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
                # A taker buy can leave fewer shares than the market minimum.
                # Quote that held size when the book has it. Do not invent the rest.
                if self.config.get('strategy')!='paper_c' and p['shares'] < D(book['min_order_size']):
                    raise ValueError('holding_below_sell_minimum')
                rate = (taker_fee_rate(market, p['row'].get('slug'))
                        if self.config.get('strategy')=='paper_c' else fee_rate(market))
                fill = quote(book,'SELL',p['shares'],rate)
                value = fill['gross']-fill['fee']
                marked += value
                record['liquidation_quote_usd'] = str(value)
            except Exception as exc:
                unknown += 1
                record['unresolved_reason'] = str(exc)
            positions.append(record)
        seen_rows = [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM seen')]
        fills = [x for x in seen_rows if x['status'] in ('PAPER_BUY','PAPER_SELL')]
        realized = sum((D(x['realized_pnl_usd']) for x in seen_rows
                        if x.get('status') in ('PAPER_SELL','PAPER_RESOLUTION') and x.get('realized_pnl_usd') is not None),ZERO)
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

    def realize_public_resolutions(self, fetch=get_json, now=None):
        """Close paper shares at a published resolution price.

        A market that is not resolved stays open. No price is substituted.
        """
        now = time.time() if now is None else now
        results = []
        for token, snapshot in list(self.holdings().items()):
            if snapshot['shares'] <= 0:
                continue
            slug = snapshot['row'].get('slug')
            outcome = snapshot['row'].get('outcome')
            try:
                market = fetch('https://gamma-api.polymarket.com/markets/slug/'+urlquote(slug,safe=''))
            except Exception as exc:
                results.append(dict(status='RESOLUTION_UNAVAILABLE',paper=True,executed=False,
                                    token_id=token,slug=slug,outcome=outcome,message=str(exc)))
                continue
            price, reason = public_resolution_price(market, outcome, token)
            if price is None:
                if reason != 'not_resolved':
                    results.append(dict(status='RESOLUTION_UNAVAILABLE',paper=True,executed=False,
                                        token_id=token,slug=slug,outcome=outcome,reason=reason))
                continue
            key = 'resolution:'+str(token)
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.contains(key):
                    continue
                position = self.holdings().get(token)
                if not position or position['shares'] <= 0:
                    continue
                closed_shares = position['shares']
                proceeds = closed_shares*price
                cost = position['cost']
                realized = proceeds-cost
                cash = self.cash+proceeds
                position['shares'] = ZERO
                position['cost'] = ZERO
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
                held_after = sum((p['shares'] for p in self.holdings().values()),ZERO)
                decision = dict(status='PAPER_RESOLUTION',paper=True,executed=False,side='RESOLVE',
                                slug=slug,outcome=outcome,token_id=token,shares=str(closed_shares),
                                resolution_price=str(price),proceeds_usd=str(proceeds),cost_usd=str(cost),
                                realized_pnl_usd=str(realized),cash_usd=str(cash),
                                price_source='gamma.outcomePrices',invented_price=False,
                                umaResolutionStatus=market.get('umaResolutionStatus'),
                                outcomePrices=market.get('outcomePrices'),outcomes=market.get('outcomes'),
                                closedTime=market.get('closedTime'),
                                decision_latency_seconds=None,
                                latency_note='Public resolution is not one of his fills, so there is no fill-to-action latency.',
                                his_price=None,our_price=str(price),cent_gap=None,rule_skipped=False)
                if held_after == 0:
                    decision['unrealized_pnl_usd'] = '0'
                    decision['equity_usd'] = str(cash)
                    goal = paper_goal(self.config)
                    decision['goal_usd'] = str(goal)
                    decision['equity_reached_goal'] = cash >= goal
                    if cash >= goal:
                        decision['seconds_from_start_to_goal'] = mark_goal_reached(self, now, cash, goal)
                self.db.execute('INSERT INTO seen VALUES (?,?)',(key,json.dumps(decision)))
                results.append(decision)
        return results

    def apply_one_rule_reset(self):
        """Reset cash, drop the open book, and tighten copied sells by one rule.

        Used only after cash plus open value has drifted down across a Paper C
        sample. The no-new-buy filter stays in place.
        """
        if self.config.get('strategy')!='paper_c':
            raise ValueError('rule reset is only defined for paper_c')
        if self.config.get('sell_must_match_his_price'):
            raise ValueError('already changed one rule')
        self.config['sell_must_match_his_price'] = True
        cash = str(D(self.config['starting_cash_usd']))
        encoded = json.dumps(self.config, sort_keys=True)
        with self.db:
            self.db.execute("UPDATE meta SET value=? WHERE key='config'",(encoded,))
            self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(cash,))
            self.db.execute('DELETE FROM positions')
        return dict(status='RULE_RESET',paper=True,executed=False,cash_usd=cash,open_book_dropped=True,
                    reset_to_starting_cash=True,rule_changed='sell_must_match_his_price',
                    why=('Cash plus open value drifted down across the sample, so cash was reset '
                         'to the starting balance and the open book was dropped. The one rule change '
                         'is that a copied sell must fill at or above his price and still net a gain '
                         'versus paper cost. New buys stay closed.'))


def decision_view(decision, window):
    return dict(window=window,action=decision.get('status'),reason=decision.get('reason'),
                his_trade=dict(side=decision.get('side'),slug=decision.get('slug'),outcome=decision.get('outcome'),
                               size=decision.get('source_size'),price=decision.get('his_price'),
                               transaction=decision.get('source_transaction'),
                               timestamp=decision.get('source_timestamp_seconds')),
                his_price=decision.get('his_price'),our_price=decision.get('our_price'),
                cent_gap=decision.get('cent_gap'),realized_pnl_usd=decision.get('realized_pnl_usd','0'),
                unrealized_pnl_usd=decision.get('unrealized_pnl_usd'),
                decision_latency_seconds=decision.get('decision_latency_seconds'),
                fee_usd=decision.get('fee'),fee_collected_in=decision.get('fee_collected_in'),
                share_fee=decision.get('share_fee'),
                copied_sell=decision.get('status')=='PAPER_SELL',rule_skipped=decision.get('rule_skipped'),
                price_note=decision.get('price_note'))


def run_paper_c(args, config, journal, observer_start, source_start):
    """Several short live windows. Decide each new trade before the next poll wait."""
    run_started_wall = time.time()
    observations, session_decisions, windows, equity_samples = {}, [], [], []
    rule_reset = None
    started = time.monotonic()
    goal = paper_goal(config)
    bid_gate = bool(config.get('skip_buy_unless_best_bid_sells_above_cost'))
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=4) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)

        announced_limits = {}

        def note_rate_limit(exc):
            key = (exc.host, exc.retry_at)
            if announced_limits.get(exc.host) == key:
                return
            announced_limits[exc.host] = key
            emit(dict(status='RATE_LIMIT', paper=True, executed=False, live_orders=False, host=exc.host,
                      message='HTTP 429. The shared client is backing off and will not poll this host again until the backoff ends.'))

        goal_state = {'seconds': None, 'stop': False}

        def consider_goal(now, equity):
            marked = mark_goal_reached(journal, now, equity, goal)
            if marked is not None:
                goal_state['seconds'] = marked
                goal_state['stop'] = True
            return marked

        def remember(window_index, decision):
            if decision.get('status')=='DUPLICATE' or decision.get('reason')=='resume_backlog_not_copied':
                return
            session_decisions.append(decision)
            window_decisions.append(decision_view(decision, window_index))
            if decision.get('seconds_from_start_to_goal') is not None:
                goal_state['seconds'] = decision['seconds_from_start_to_goal']
                goal_state['stop'] = True
            elif decision.get('status') in ('PAPER_BUY', 'PAPER_SELL', 'PAPER_RESOLUTION'):
                consider_goal(time.time(), journal.cash)

        def note_same_minute_sell(exited, window_index):
            if not exited:
                return
            if exited.get('unrealized_pnl_usd') is None:
                port = journal.portfolio()
                equity = quoted_equity(port)
                exited['unrealized_pnl_usd'] = port.get('unrealized_pnl_at_liquidation_quote_usd')
                exited['equity_usd'] = None if equity is None else str(equity)
                exited['goal_usd'] = str(goal)
                exited['equity_reached_goal'] = equity is not None and equity >= goal
                exited['seconds_from_start_to_goal'] = consider_goal(time.time(), journal.cash)
            elif exited.get('seconds_from_start_to_goal') is not None:
                goal_state['seconds'] = exited['seconds_from_start_to_goal']
                goal_state['stop'] = True
            emit(exited)
            remember(window_index, exited)

        def minute_position_open():
            for position in journal.holdings().values():
                if position['shares'] > 0:
                    return True
            return False

        def sell_same_minute(window_index, market=None, book=None):
            if not config.get('sell_same_minute_if_bid_above_cost'):
                return
            books = []
            if market is not None and book is not None:
                books.append((market, book))
            else:
                if public_client.retry_at('clob.polymarket.com') is not None:
                    return
                for token, position in list(journal.holdings().items()):
                    if position['shares'] <= 0:
                        continue
                    try:
                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        books.append((market_future.result(), book_future.result()))
                    except RateLimited as exc:
                        note_rate_limit(exc)
                        return
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc),token_id=token))
            for market_row, book_row in books:
                try:
                    note_same_minute_sell(
                        journal.realize_same_minute_if_bid_above_cost(market_row, book_row, time.time()),
                        window_index)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))

        open_shares = sum((p['shares'] for p in journal.holdings().values()), ZERO)
        if bid_gate:
            sample_reset = False
            rule_changed = 'copy_his_exact_printed_size'
            rule_changed_this_run = True
            rule_change_why = ('The bid gate copied no buys, so cash stayed at the start. '
                               'A copy is now his exact printed share count, same side and market, only when that full size is on the book at his price or better. '
                               'Under 5 shares, a missing size, or a cost above the cash is a skip. Nothing is scaled and no fill is invented. '
                               'The position is held while the best bid is above paper cost, then the size posted at that bid is sold once the bid is no longer above cost. '
                               'Every fill logs his transaction hash and decision latency. Starting cash stays $37.40. No live orders.')
        else:
            sample_reset = (bool(config.get('sell_same_minute_if_bid_above_cost'))
                            and journal.cash == D(config['starting_cash_usd']) and open_shares == 0)
            rule_changed = ('sell_same_minute_if_bid_above_cost' if sample_reset
                            else None if rule_reset is None else rule_reset['rule_changed'])
            rule_changed_this_run = rule_reset is not None or sample_reset
            rule_change_why = (('The previous book was steadily losing, so cash was reset to $39 and the open positions were dropped. '
                                'The one rule change is to sell a paper position when the bid is above paper cost in the same minute it opened, without waiting for a sell he prints.')
                               if sample_reset else None if rule_reset is None else rule_reset['why'])
        buy_filter = ('copy his exact printed share count, same side and market, only when the full size is on the book at his price or better; '
                      'skip when the size is under 5 shares, not on the book, or cash cannot cover it; '
                      'never scale a fill and never pay a worse price; '
                      'a new buy pays the taker fee in shares and a new sell pays it from USDC proceeds; '
                      'copy a sell he prints only when it closes an existing paper position above paper cost')
        if config.get('sell_same_minute_if_bid_above_cost'):
            buy_filter += '; hold while the best bid is above paper cost, then sell the size posted at that bid once it is no longer above cost'
        started_clock = book_started_at(journal, run_started_wall)
        emit(dict(status='STARTED',strategy='paper_c',paper=True,executed=False,live_orders=False,
                  leader_wallet=config['leader_wallet'],starting_cash_usd=str(D(config['starting_cash_usd'])),
                  resumed_cash_usd=str(journal.cash),goal_usd=str(goal),
                  book_started_at_utc=datetime.fromtimestamp(started_clock,timezone.utc).isoformat(),
                  rule_changed_this_run=rule_changed_this_run,
                  reset_this_run=sample_reset,
                  rule_changed=rule_changed,
                  second_rule_changed=False,
                  observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  poll_seconds=config['poll_seconds'],windows=args.windows,window_seconds=args.duration,
                  latency='seconds from his fill timestamp to the paper copy or skip',
                  filter=buy_filter))
        for close in journal.realize_public_resolutions(get_json, time.time()):
            emit(close)
            if close.get('status')=='PAPER_RESOLUTION':
                session_decisions.append(close)
                if close.get('seconds_from_start_to_goal') is not None:
                    goal_state['seconds'] = close['seconds_from_start_to_goal']
                    goal_state['stop'] = True
        opening = journal.portfolio()
        opening_equity = quoted_equity(opening)
        if opening_equity is not None:
            equity_samples.append(opening_equity)
        consider_goal(time.time(), journal.cash)
        live_queue = queue.Queue()

        def start_trade_stream():
            """Public activity stream. No private key. His wallet is filtered here."""
            try:
                import websocket
            except Exception as exc:
                emit(dict(status='STREAM_UNAVAILABLE', paper=True, executed=False, live_orders=False,
                          message=str(exc)))
                return
            wallet = config['leader_wallet']

            def on_open(ws):
                ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "activity", "type": "orders_matched", "filters": ""},
                    {"topic": "activity", "type": "trades", "filters": ""},
                ]}))

                def beat():
                    while True:
                        time.sleep(5)
                        try:
                            ws.send('PING')
                        except Exception:
                            return
                threading.Thread(target=beat, daemon=True).start()

            def on_message(ws, message):
                if not message or message in ('PONG', 'pong'):
                    return
                try:
                    data = json.loads(message)
                except Exception:
                    return
                payload = data.get('payload') if isinstance(data, dict) else None
                row = leader_trade_row(payload, wallet)
                if row is not None:
                    live_queue.put(row)

            def run():
                while True:
                    try:
                        websocket.WebSocketApp(
                            'wss://ws-live-data.polymarket.com',
                            on_open=on_open, on_message=on_message,
                        ).run_forever()
                    except Exception:
                        pass
                    time.sleep(1)
            threading.Thread(target=run, daemon=True).start()
            emit(dict(status='STREAM', paper=True, executed=False, live_orders=False,
                      source='wss://ws-live-data.polymarket.com', topic='activity',
                      note='Public trades only. No private key.'))

        start_trade_stream()
        emit(dict(status='RESUMED',paper=True,executed=False,cash_usd=str(journal.cash),
                  equity_usd=None if opening_equity is None else str(opening_equity),
                  unrealized_pnl_usd=opening.get('unrealized_pnl_at_liquidation_quote_usd'),
                  realized_pnl_usd=opening.get('realized_pnl_usd'),
                  goal_usd=str(goal),
                  equity_reached_goal=opening_equity is not None and opening_equity>=goal,
                  seconds_from_start_to_goal=goal_state['seconds']))
        def handle_trades(rows, window_index):
            for key,row in row_keys(rows):
                if journal.contains(key):
                    continue
                try:
                    if journal.contains(trade_identity(row)):
                        continue
                except (ArithmeticError, TypeError, ValueError, KeyError):
                    pass
                if (row.get('proxy_wallet','').lower()!=config['leader_wallet'].lower()
                    or row.get('type')!='TRADE' or row.get('is_combo') or row.get('side') not in ('BUY','SELL')
                    or market_timeframe(row.get('slug')) not in config['timeframes_minutes']
                    or int(row['timestamp']) < observer_start):
                    continue
                appeared = time.time()
                if key not in observations:
                    delay = appeared-int(row['timestamp'])
                    observations[key]={'delay':delay,'continuous_sample':int(row['timestamp'])>=run_started_wall}
                    emit(dict(status='OBSERVED',event_id=key,side=row['side'],slug=row['slug'],
                              outcome=row.get('outcome'),price=row.get('price'),
                              source_transaction=row.get('transaction_hash'),
                              source_timestamp_seconds=row['timestamp'],
                              first_seen_at_utc=datetime.fromtimestamp(appeared,timezone.utc).isoformat(),
                              source_to_first_seen_seconds=round(delay,3),
                              continuous_run_latency_sample=observations[key]['continuous_sample'],
                              seen_before_run_start=not observations[key]['continuous_sample']))
                try:
                    if int(row['timestamp']) < run_started_wall:
                        decision = journal.process(key,row,{},{},appeared,
                                                   skip_reason='resume_backlog_not_copied')
                        emit(decision)
                        remember(window_index, decision)
                    else:
                        held = journal.holdings().get(str(row['token_id']))
                        sell_can_close = row['side']=='SELL' and held and held['shares']>0
                        # A buy needs the book the moment it appears, to see if
                        # the ask is at his price or better. A sell with no
                        # position is still skipped before that read.
                        needs_book = sell_can_close or (row['side']=='BUY' and config.get('copy_buys_at_or_better'))
                        book_limited = public_client.retry_at('clob.polymarket.com')
                        if needs_book and book_limited is not None:
                            note_rate_limit(RateLimited('clob.polymarket.com', book_limited))
                            decision=journal.process(key,row,{},{},appeared,skip_reason='rate_limited')
                            emit(decision)
                            remember(window_index, decision)
                        elif needs_book:
                            market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
                            book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
                            market,book=market_future.result(),book_future.result()
                            acted=time.time()
                            decision=journal.process(key,row,market,book,acted)
                            emit(decision)
                            remember(window_index, decision)
                            if decision.get('status')=='PAPER_BUY':
                                sell_same_minute(window_index, market, book)
                                sell_same_minute(window_index)
                        else:
                            reason=('paper_c_no_new_buys' if row['side']=='BUY'
                                    else 'no_matching_paper_position')
                            decision=journal.process(key,row,{},{},appeared,skip_reason=reason)
                            emit(decision)
                            remember(window_index, decision)
                except RateLimited as exc:
                    note_rate_limit(exc)
                    decision=journal.process(key,row,{},{},appeared,skip_reason='rate_limited')
                    emit(decision)
                    remember(window_index, decision)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc),event_id=key))

        try:
            for window_index in range(1, args.windows+1):
                if goal_state['stop']:
                    break
                window_started = time.monotonic()
                window_decisions = []
                next_resolution = time.monotonic()
                emit(dict(status='WINDOW_START',window=window_index,cash_usd=str(journal.cash),
                          paper=True,executed=False))
                pending_activity = None
                while time.monotonic()-window_started < args.duration and not goal_state['stop']:
                    next_request = time.monotonic()+config['poll_seconds']
                    # A same-minute position sells on the first book whose bid clears
                    # cost. The activity request must not hold that check.
                    if minute_position_open():
                        sell_same_minute(window_index)
                    try:
                        if pending_activity is None:
                            pending_activity=pool.submit(activity,config['leader_wallet'],max(source_start,int(time.time())-120),int(time.time()))
                            rows=[]
                        elif pending_activity.done():
                            rows=list(pending_activity.result())
                            pending_activity=pool.submit(activity,config['leader_wallet'],max(source_start,int(time.time())-120),int(time.time()))
                        else:
                            rows=[]
                        queued=[]
                        while True:
                            try:
                                queued.append(live_queue.get_nowait())
                            except queue.Empty:
                                break
                        if rows or queued:
                            handle_trades(list(rows)+queued, window_index)
                    except RateLimited as exc:
                        note_rate_limit(exc)
                        pending_activity=None
                        remaining=args.duration-(time.monotonic()-window_started)
                        pause=min(max(0, exc.retry_at-time.monotonic()), max(0, remaining))
                        if pause > 0:
                            time.sleep(pause)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                        pending_activity=None
                        next_request=max(next_request,time.monotonic()+0.5)
                    sell_same_minute(window_index)
                    if time.monotonic() >= next_resolution and not goal_state['stop']:
                        next_resolution = time.monotonic()+30
                        if any(p['shares'] > 0 for p in journal.holdings().values()):
                            for close in journal.realize_public_resolutions(get_json, time.time()):
                                emit(close)
                                if close.get('status')=='PAPER_RESOLUTION':
                                    session_decisions.append(close)
                                    window_decisions.append(decision_view(close, window_index))
                                    if close.get('seconds_from_start_to_goal') is not None:
                                        goal_state['seconds'] = close['seconds_from_start_to_goal']
                                        goal_state['stop'] = True
                                    else:
                                        consider_goal(time.time(), journal.cash)
                    remaining=args.duration-(time.monotonic()-window_started)
                    if remaining > 0 and not minute_position_open() and not goal_state['stop']:
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
                for close in journal.realize_public_resolutions(get_json, time.time()):
                    emit(close)
                    if close.get('status')=='PAPER_RESOLUTION':
                        session_decisions.append(close)
                        window_decisions.append(decision_view(close, window_index))
                        if close.get('seconds_from_start_to_goal') is not None:
                            goal_state['seconds'] = close['seconds_from_start_to_goal']
                            goal_state['stop'] = True
                port = journal.portfolio()
                equity = quoted_equity(port)
                consider_goal(time.time(), journal.cash)
                if equity is not None:
                    equity_samples.append(equity)
                window_record = dict(status='WINDOW',window=window_index,trades=len(window_decisions),
                                     note=None if window_decisions else 'No trade in this window.',
                                     decisions=window_decisions,cash_usd=port['cash_usd'],
                                     realized_pnl_usd=port['realized_pnl_usd'],
                                     unrealized_pnl_usd=port.get('unrealized_pnl_at_liquidation_quote_usd'),
                                     equity_usd=None if equity is None else str(equity),
                                     open_cost_usd=port['open_cost_usd'],paper=True,executed=False)
                windows.append(window_record)
                emit(window_record)
        except KeyboardInterrupt:
            pass
        final = journal.portfolio()
        emit(final)
        fill_latencies=[d.get('decision_latency_seconds') for d in session_decisions
                        if d.get('status') in ('PAPER_BUY','PAPER_SELL')
                        and isinstance(d.get('decision_latency_seconds'),(int,float))
                        and d['decision_latency_seconds']>=0]
        copies = sum(d.get('status') in ('PAPER_BUY','PAPER_SELL') for d in session_decisions)
        skips = sum(d.get('status')=='SKIP' for d in session_decisions)
        session_fees = sum((D(d.get('fee') or '0') for d in session_decisions
                             if d.get('status') in ('PAPER_BUY','PAPER_SELL')), ZERO)
        goal_seconds = mark_goal_reached(journal, time.time(), journal.cash, goal)
        summary=dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),
                     windows=len(windows),decisions=len(session_decisions),
                     copies=copies,skips=skips,fees_usd=str(session_fees),
                     cash_usd=final['cash_usd'],realized_pnl_usd=final['realized_pnl_usd'],
                     sells_copied=sum(d.get('status')=='PAPER_SELL' for d in session_decisions),
                     buys_copied=sum(d.get('status')=='PAPER_BUY' for d in session_decisions),
                     median_buy_latency_seconds=median_latency([
                         d.get('decision_latency_seconds') for d in session_decisions
                         if d.get('status')=='PAPER_BUY']),
                     open_cost_usd=final.get('open_cost_usd'),
                     cash_reached_75=journal.cash >= goal,
                     decision_latency_seconds=fill_latencies,
                     decision_latency_min_seconds=min(fill_latencies) if fill_latencies else None,
                     decision_latency_max_seconds=max(fill_latencies) if fill_latencies else None,
                     reset_to_starting_cash=False if bid_gate else rule_reset is not None or sample_reset,
                     rule_changed=rule_changed,
                     second_rule_changed=False,
                     goal_usd=str(goal),
                     seconds_from_start_to_goal=goal_seconds,
                     note='Paper fills only. No live orders. Latency is his fill timestamp to the copy or skip.')
        emit(summary)
    if args.result:
        ending_equity = quoted_equity(final)
        seen_rows = [json.loads(item[0]) for item in journal.db.execute('SELECT payload FROM seen')]
        book_buys = [item for item in seen_rows if item.get('status')=='PAPER_BUY']
        book_fees = sum((D(item.get('fee') or '0') for item in seen_rows
                         if item.get('status') in ('PAPER_BUY','PAPER_SELL')), ZERO)
        open_cost = sum((position['cost'] for position in journal.holdings().values()), ZERO)
        goal_reached = journal.cash >= goal
        skip_reasons = {}
        for decision in session_decisions:
            if decision.get('status')=='SKIP':
                reason = decision.get('reason') or 'unspecified'
                skip_reasons[reason] = skip_reasons.get(reason, 0)+1
        result=dict(name='Paper C',paper_only=True,live_orders=False,private_keys_used=False,brez_used=False,
                    leader_wallet=config['leader_wallet'],leader_handle='@bosona',
                    starting_cash_usd=str(D(config['starting_cash_usd'])),
                    ending_cash_usd=final['cash_usd'],realized_pnl_usd=final['realized_pnl_usd'],
                    fees_usd=str(book_fees),session_fees_usd=str(session_fees),
                    total_simulated_fees_usd=final.get('total_simulated_fees_usd'),
                    open_cost_usd=str(open_cost),
                    buys=len(book_buys),
                    median_buy_latency_seconds=median_latency([
                        item.get('decision_latency_seconds') for item in book_buys]),
                    cash_reached_75=goal_reached,
                    copies=copies,skips=skips,skip_reasons=skip_reasons,
                    unrealized_pnl_usd=final.get('unrealized_pnl_at_liquidation_quote_usd'),
                    equity_usd=None if ending_equity is None else str(ending_equity),
                    any_sell_copied=any(d.get('status')=='PAPER_SELL' for d in session_decisions),
                    buys_copied=sum(d.get('status')=='PAPER_BUY' for d in session_decisions),
                    sells_copied=sum(d.get('status')=='PAPER_SELL' for d in session_decisions),
                    decisions=len(session_decisions),
                    fill_decision_latency_seconds=[dict(status=d.get('status'), side=d.get('side'),
                                                        slug=d.get('slug'), fee_usd=d.get('fee'),
                                                        cash_usd=d.get('cash_usd'),
                                                        leader_transaction_hash=d.get('leader_transaction_hash'),
                                                        decision_latency_seconds=d.get('decision_latency_seconds'))
                                                   for d in session_decisions
                                                   if d.get('status') in ('PAPER_BUY', 'PAPER_SELL')],
                    decision_latency_seconds=[dict(window=view['window'],side=view['his_trade']['side'],
                                                   slug=view['his_trade']['slug'],action=view['action'],
                                                   reason=view['reason'],his_price=view['his_price'],
                                                   our_price=view['our_price'],cent_gap=view['cent_gap'],
                                                   realized_pnl_usd=view['realized_pnl_usd'],
                                                   unrealized_pnl_usd=view['unrealized_pnl_usd'],
                                                   fee_usd=view.get('fee_usd'),
                                                   fee_collected_in=view.get('fee_collected_in'),
                                                   share_fee=view.get('share_fee'),
                                                   latency_seconds=view['decision_latency_seconds'])
                                              for window in windows for view in window['decisions']],
                    reset_to_39=False if bid_gate else rule_reset is not None or sample_reset,
                    rule_changed_this_run=rule_changed_this_run,
                    rule_changed=rule_changed,
                    rule_change_why=rule_change_why,
                    second_rule_changed=False,
                    closes=[dict(outcome=d.get('outcome'),slug=d.get('slug'),shares=d.get('shares'),
                                 resolution_price=d.get('resolution_price'),proceeds_usd=d.get('proceeds_usd'),
                                 cost_usd=d.get('cost_usd'),realized_pnl_usd=d.get('realized_pnl_usd'),
                                 cash_usd=d.get('cash_usd'),unrealized_pnl_usd=d.get('unrealized_pnl_usd'),
                                 equity_usd=d.get('equity_usd'),goal_usd=d.get('goal_usd'),
                                 equity_reached_goal=d.get('equity_reached_goal'),
                                 decision_latency_seconds=d.get('decision_latency_seconds'),
                                 latency_note=d.get('latency_note'),price_source=d.get('price_source'),
                                 reason=d.get('reason'),our_price=d.get('our_price'))
                            for d in session_decisions
                            if d.get('status')=='PAPER_RESOLUTION' or d.get('status')=='PAPER_SELL'],
                    book_was_steadily_losing=rule_reset is not None,
                    goal_usd=str(goal),goal_reached=goal_reached,
                    seconds_from_start_to_goal=goal_seconds,poll_seconds=config['poll_seconds'],
                    latency_definition='seconds from his fill timestamp to the paper copy or skip',
                    filter=('Copy his exact printed share count, same side and market, only when the full size is on the book at his price or better. '
                            'Skip when the size is under 5 shares, not on the book, or cash cannot cover it. Never scale a fill and never pay a worse price. '
                            'A new fill is a taker. The fee is shares times feeRate times price times one minus price, rounded to five decimals. '
                            'On a buy that fee is taken in shares. On a sell it comes out of the USDC proceeds. A resolution has no fee. '
                            'Every fill logs his transaction hash and decision latency. '
                            'Copy a sell he prints only when it closes an existing paper position above paper cost. '
                            + ('Hold while the best bid is above paper cost, then sell the size posted at that bid once it is no longer above cost. '
                               if config.get('sell_same_minute_if_bid_above_cost') else '')
                            if config.get('copy_buys_at_or_better') else
                            'Do not open new buys. Copy a sell only when it closes an existing paper position above paper cost. Skip when there is no matching paper position.'),
                    windows=[dict(window=window['window'],trades=window['trades'],note=window['note'],
                                  his_trades=window['decisions'],cash_usd=window['cash_usd'],
                                  realized_pnl_usd=window['realized_pnl_usd'],
                                  unrealized_pnl_usd=window['unrealized_pnl_usd'])
                             for window in windows])
        Path(args.result).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(dict(status='RESULT_WRITTEN',path=args.result)),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.paper48.json')
    parser.add_argument('--db',default='paper48.sqlite3')
    parser.add_argument('--output',default='paper48.jsonl')
    parser.add_argument('--duration',type=float,default=120)
    parser.add_argument('--windows',type=int,default=1)
    parser.add_argument('--result',default=None)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error('duration must be positive and finite')
    if args.windows < 1:
        parser.error('windows must be at least 1')
    raw_config = json.loads(Path(args.config).read_text())
    interval = float(D(raw_config['poll_seconds']))
    floor = 0.05 if raw_config.get('strategy')=='paper_c' else 0.25
    if not math.isfinite(interval) or interval < floor:
        raise ValueError('paper poll_seconds must be finite and at least '+str(floor))
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
    if not stored:
        journal.baseline(activity(config['leader_wallet'],source_start,int(time.time())),observer_start)
    if config.get('strategy')=='paper_c':
        run_paper_c(args,config,journal,observer_start,source_start)
        return
    started, run_started_wall, observations = time.monotonic(),time.time(),{}
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=2) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)
        emit(dict(status='STARTED',starting_cash_usd=str(config['starting_cash_usd']),cash_usd=str(journal.cash),
                  paper=True,executed=False,observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  poll_seconds=config['poll_seconds']))
        try:
            while time.monotonic()-started < args.duration:
                next_request = time.monotonic()+config['poll_seconds']
                try:
                    rows=activity(config['leader_wallet'],max(source_start,int(time.time())-120),int(time.time()))
                    for key,row in row_keys(rows):
                        if journal.contains(key):
                            continue
                        if (row.get('proxy_wallet','').lower()!=config['leader_wallet'].lower()
                            or row.get('type')!='TRADE' or row.get('is_combo') or row.get('side') not in ('BUY','SELL')
                            or market_timeframe(row.get('slug')) not in config['timeframes_minutes']
                            or int(row['timestamp']) < observer_start):
                            continue
                        if key not in observations:
                            observations[key]={'delay':time.time()-int(row['timestamp']),
                                               'continuous_sample':int(row['timestamp'])>=run_started_wall}
                            emit(dict(status='OBSERVED',event_id=key,side=row['side'],slug=row['slug'],
                                      source_transaction=row.get('transaction_hash'),source_timestamp_seconds=row['timestamp'],
                                      first_seen_at_utc=datetime.now(timezone.utc).isoformat(),
                                      source_to_first_seen_seconds=round(observations[key]['delay'],3),
                                      continuous_run_latency_sample=observations[key]['continuous_sample'],
                                      seen_before_run_start=not observations[key]['continuous_sample']))
                        backlog = int(row['timestamp']) < run_started_wall
                        reason=('resume_backlog_not_copied' if backlog else
                                source_skip(dict(row,side='BUY'),config,time.time()))
                        if reason:
                            market=book={}
                        else:
                            market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
                            book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
                            market,book=market_future.result(),book_future.result()
                        emit(journal.process(key,row,market,book,time.time(),
                                             skip_reason='resume_backlog_not_copied' if backlog else None))
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))
                    next_request=max(next_request,time.monotonic()+2)
                for token,position in list(journal.holdings().items()):
                    if position['shares'] <= 0:
                        continue
                    try:
                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        exited=journal.realize_if_bid_above_cost(market_future.result(),book_future.result(),time.time())
                        if exited:
                            emit(exited)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                remaining=args.duration-(time.monotonic()-started)
                if remaining > 0:
                    time.sleep(min(max(0,next_request-time.monotonic()),remaining))
        except KeyboardInterrupt:
            pass
        emit(journal.portfolio())
        delays=sorted(v['delay'] for v in observations.values() if v['continuous_sample'] and v['delay']>=0)
        emit(dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),observations=len(observations),
                  continuous_run_latency_samples=len(delays),
                  first_seen_delay_min_seconds=min(delays) if delays else None,
                  first_seen_delay_max_seconds=max(delays) if delays else None,
                  note='Snapshot depth simulations; no live fills, settlement or guaranteed execution latency.'))


if __name__=='__main__':
    main()
