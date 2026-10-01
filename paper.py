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
the same side and market at his price or better. One buy cannot spend more
than half of current cash, and it never buys more shares than he did. A better
price is a copy. A worse price is not. The market minimum is still 5 shares.
If half the cash cannot buy that minimum at his price or better, the buy is
skipped. The copy is a taker. Crypto taker fee is 0.07. On a new buy the fee
is taken in shares, so the held count is the ordered size minus fee divided by
price. On a sell the fee comes out of the USDC proceeds. A resolution is not a
fill and has no fee. Makers pay nothing. In the same minute a paper position
opens, a bid above paper cost sells that position without waiting for a sell
he prints. Decision latency is his fill timestamp to that copy or skip. The
paper48 path is unchanged.
"""
import argparse
import json
import math
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from urllib.parse import quote as urlquote

from bot import activity, array, decimal as D, get_json, market_timeframe, row_keys, source_skip, validate

ZERO = Decimal(0)
STEP = Decimal('0.01')
GOAL = Decimal('75')


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


def half_cash_buy_size(book, his_size, spend_cap, ceiling, minimum):
    """Largest buy that stays within half of cash and within his size.

    Asks above his price are ignored. The returned size is his exact size when
    that size fits. Otherwise it steps down to the cent. Below the 5-share
    minimum raises a skip reason and does not invent a smaller fill.
    """
    floor = max(minimum, FIVE)
    if his_size < floor:
        raise ValueError('below_market_minimum')
    if spend_cap <= 0:
        raise ValueError('half_cash_below_five_shares')
    asks = levels(book, 'BUY')
    if not asks:
        raise ValueError('his_size_not_on_the_book')
    good = [level for level in asks if level[0] <= ceiling]
    if not good:
        raise ValueError('latency_worse_than_leader_price')
    taken, spent = ZERO, ZERO
    for price, available in good:
        room_shares = his_size - taken
        room_cash = spend_cap - spent
        if room_shares <= 0 or room_cash <= 0:
            break
        take = min(available, room_shares)
        cost = take * price
        if cost > room_cash:
            take = room_cash / price
            cost = take * price
            if cost > room_cash:
                take = (room_cash / price).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                cost = take * price
        if take <= 0:
            break
        taken += take
        spent += cost
        if take + ZERO < available:
            break
    if taken == his_size and spent <= spend_cap:
        quantity = his_size
    else:
        quantity = min(taken, his_size).quantize(STEP, rounding=ROUND_DOWN)
    if quantity < floor:
        depth = sum((size for _, size in good), ZERO)
        if depth >= floor and floor * good[0][0] > spend_cap:
            raise ValueError('half_cash_below_five_shares')
        raise ValueError('five_shares_not_at_or_better_than_leader_fill')
    return quantity


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
                    # One buy spends at most half of the cash on hand, and never
                    # more shares than he bought. The fill has to be his price
                    # or better. A worse ask is a skip. The taker fee is taken
                    # in shares, not as extra USDC.
                    half = self.cash / 2
                    decision['spend_cap_usd'] = str(half)
                    decision['his_size'] = str(source_shares)
                    try:
                        quantity = half_cash_buy_size(book, source_shares, half, source_price, minimum)
                    except ValueError as exc:
                        if str(exc) == 'latency_worse_than_leader_price':
                            asks = levels(book, 'BUY')
                            if asks:
                                decision['simulated_vwap'] = str(asks[0][0])
                        raise
                    try:
                        fill = quote(book, 'BUY', quantity, rate, source_price)
                    except ValueError:
                        raise ValueError('five_shares_not_at_or_better_than_leader_fill')
                    decision['simulated_vwap'] = str(fill['vwap'])
                    if fill['vwap'] > source_price:
                        raise ValueError('latency_worse_than_leader_price')
                    if quantity > source_shares:
                        raise ValueError('larger_than_his_size')
                    debit = fill['gross']
                    if debit > half:
                        raise ValueError('half_cash_exceeded')
                    received = quantity - fill['share_fee']
                    if received <= 0:
                        raise ValueError('taker_fee_consumed_the_shares')
                    cash = self.cash - debit
                    opening = position['shares'] == 0
                    position['shares'] += received
                    position['cost'] += debit
                    fill = dict(fill, shares=received)
                    decision['ordered_shares'] = str(quantity)
                    decision['fee_collected_in'] = 'shares'
                    decision['share_fee'] = str(fill['share_fee'])
                    decision['scaled_below_his_size'] = quantity < source_shares
                    if opening:
                        position['opened_at'] = now
                        position['opened_source_timestamp'] = row['timestamp']
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
            held_after = sum((p['shares'] for tok,p in positions.items() if tok!=token),ZERO)+position['shares']
            if held_after==0:
                decision['unrealized_pnl_usd'] = '0'
            if decision['status']=='SKIP' and 'realized_pnl_usd' not in decision:
                decision['realized_pnl_usd'] = '0'
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

    def realize_same_minute_if_bid_above_cost(self, market, book, now):
        """Sell a paper position when the bid clears cost in its opening minute.

        This does not wait for a sell he prints. A later minute does not sell.
        The book must hold the full position. Missing size is not invented.
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
                opened = position.get('opened_at')
                if opened is None or int(D(opened)) // 60 != int(now) // 60:
                    return None
                if book.get('market') != position['row'].get('condition_id'):
                    return None
                age = D(now)*1000-D(book['timestamp'])
                if age < -1000 or age > D(self.config['max_book_age_seconds'])*1000:
                    return None
                rate = taker_fee_rate(market, position['row'].get('slug'))
                minimum = D(book['min_order_size'])
                quantity = position['shares']
                if minimum <= 0 or quantity <= 0:
                    return None
                cost_per = position['cost']/quantity
                try:
                    fill = quote(book,'SELL',quantity,rate,cost_per)
                except ValueError:
                    return None
                removed = position['cost']
                net = fill['gross']-fill['fee']
                if fill['vwap'] <= cost_per or net <= removed:
                    return None
                cash = self.cash+net
                position['shares'] = ZERO
                position['cost'] = ZERO
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                source_ts = position.get('opened_source_timestamp', position['row'].get('timestamp'))
                latency = None if source_ts is None else round(now-int(source_ts), 3)
                decision = dict(status='PAPER_SELL',reason='same_minute_bid_above_paper_cost',rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),our_price=str(fill['vwap']),his_price=None,cent_gap=None,
                                realized_pnl_usd=str(net-removed),cash_usd=str(cash),held_shares='0',
                                cost_per_share_usd=str(cost_per),cost_usd=str(removed),
                                source_timestamp_seconds=source_ts,decision_latency_seconds=latency,
                                latency_note=('The bid was above paper cost in the same minute the position opened. '
                                              'This sell is not one of his prints. Latency is his opening fill to this sell.'))
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
                held_after = sum((p['shares'] for p in self.holdings().values()), ZERO)
                if held_after == 0:
                    decision['unrealized_pnl_usd'] = '0'
                    decision['equity_usd'] = str(cash)
                    decision['equity_reached_75'] = cash >= GOAL
                self.db.execute('INSERT INTO seen VALUES (?,?)',
                                (f"same-minute:{token}:{int(D(opened))}",json.dumps(decision)))
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
                    decision['equity_reached_75'] = cash >= GOAL
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
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=4) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)

        def remember(window_index, decision):
            if decision.get('status')=='DUPLICATE' or decision.get('reason')=='resume_backlog_not_copied':
                return
            session_decisions.append(decision)
            window_decisions.append(decision_view(decision, window_index))

        def note_same_minute_sell(exited, window_index):
            if not exited:
                return
            if exited.get('unrealized_pnl_usd') is None:
                port = journal.portfolio()
                if not port.get('unresolved_positions'):
                    exited['unrealized_pnl_usd'] = port.get('unrealized_pnl_at_liquidation_quote_usd')
                    exited['equity_usd'] = port.get('equity_at_liquidation_quote_usd')
                    equity = port.get('equity_at_liquidation_quote_usd')
                    exited['equity_reached_75'] = equity is not None and D(equity) >= GOAL
            emit(exited)
            remember(window_index, exited)

        def minute_position_open():
            minute = int(time.time()) // 60
            for position in journal.holdings().values():
                opened = position.get('opened_at')
                if position['shares'] > 0 and opened is not None and int(D(opened)) // 60 == minute:
                    return True
            return False

        def sell_same_minute(window_index, market=None, book=None):
            if not config.get('sell_same_minute_if_bid_above_cost'):
                return
            books = []
            if market is not None and book is not None:
                books.append((market, book))
            else:
                minute = int(time.time()) // 60
                for token, position in list(journal.holdings().items()):
                    opened = position.get('opened_at')
                    if position['shares'] <= 0 or opened is None or int(D(opened)) // 60 != minute:
                        continue
                    try:
                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        books.append((market_future.result(), book_future.result()))
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
        sample_reset = (bool(config.get('sell_same_minute_if_bid_above_cost'))
                        and journal.cash == D(config['starting_cash_usd']) and open_shares == 0)
        buy_filter = ('one buy spends at most half of current cash and never more than his size, '
                      'same side and market, only at his price or better; '
                      'a worse price is skipped and is not copied; '
                      'the market minimum is 5 shares; crypto taker fee is 0.07; '
                      'a new buy pays the taker fee in shares and a new sell pays it from USDC proceeds; '
                      'copy a sell he prints only when it closes an existing paper position above paper cost')
        if config.get('sell_same_minute_if_bid_above_cost'):
            buy_filter += '; in the opening minute, sell when the bid is above paper cost without waiting for his sell'
        half_rule = 'one_buy_at_most_half_of_current_cash_and_never_more_than_his_size'
        emit(dict(status='STARTED',strategy='paper_c',paper=True,executed=False,live_orders=False,
                  leader_wallet=config['leader_wallet'],starting_cash_usd=str(config['starting_cash_usd']),
                  resumed_cash_usd=str(journal.cash),
                  rule_changed_this_run=sample_reset,
                  reset_this_run=sample_reset,
                  rule_changed=half_rule if sample_reset else None,
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
        opening = journal.portfolio()
        opening_equity = D(opening.get('equity_at_liquidation_quote_usd', opening['cash_usd']))
        equity_samples.append(opening_equity)
        emit(dict(status='RESUMED',paper=True,executed=False,cash_usd=str(journal.cash),
                  equity_usd=str(opening_equity),
                  unrealized_pnl_usd=opening.get('unrealized_pnl_at_liquidation_quote_usd'),
                  realized_pnl_usd=opening.get('realized_pnl_usd'),
                  equity_reached_75=opening_equity>=GOAL))
        try:
            for window_index in range(1, args.windows+1):
                window_started = time.monotonic()
                window_decisions = []
                emit(dict(status='WINDOW_START',window=window_index,cash_usd=str(journal.cash),
                          paper=True,executed=False))
                while time.monotonic()-window_started < args.duration:
                    next_request = time.monotonic()+config['poll_seconds']
                    # A same-minute position sells on the first book whose bid clears
                    # cost. The activity request must not hold that check.
                    if minute_position_open():
                        sell_same_minute(window_index)
                    try:
                        activity_future=pool.submit(activity,config['leader_wallet'],max(source_start,int(time.time())-120),int(time.time()))
                        while not activity_future.done() and minute_position_open() and time.monotonic()-window_started < args.duration:
                            watched=time.monotonic()
                            sell_same_minute(window_index)
                            pause=config['poll_seconds']-(time.monotonic()-watched)
                            if pause > 0 and not activity_future.done():
                                time.sleep(pause)
                        rows=activity_future.result()
                        for key,row in row_keys(rows):
                            if journal.contains(key):
                                continue
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
                                    if needs_book:
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
                            except Exception as exc:
                                emit(dict(status='ERROR',message=str(exc),event_id=key))
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                        next_request=max(next_request,time.monotonic()+0.5)
                    sell_same_minute(window_index)
                    remaining=args.duration-(time.monotonic()-window_started)
                    if remaining > 0 and not minute_position_open():
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
                for close in journal.realize_public_resolutions(get_json, time.time()):
                    emit(close)
                    if close.get('status')=='PAPER_RESOLUTION':
                        session_decisions.append(close)
                        window_decisions.append(decision_view(close, window_index))
                port = journal.portfolio()
                equity = None if port.get('unresolved_positions') else D(port.get('equity_at_liquidation_quote_usd',port['cash_usd']))
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
        latencies=[d['decision_latency_seconds'] for d in session_decisions
                   if isinstance(d.get('decision_latency_seconds'),(int,float)) and d['decision_latency_seconds']>=0]
        summary=dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),
                     windows=len(windows),decisions=len(session_decisions),
                     sells_copied=sum(d.get('status')=='PAPER_SELL' for d in session_decisions),
                     decision_latency_min_seconds=min(latencies) if latencies else None,
                     decision_latency_max_seconds=max(latencies) if latencies else None,
                     reset_to_starting_cash=rule_reset is not None or sample_reset,
                     rule_changed=(half_rule if sample_reset
                                   else None if rule_reset is None else rule_reset['rule_changed']),
                     second_rule_changed=False,
                     note='Paper fills only. No live orders. Latency is his fill timestamp to the copy or skip.')
        emit(summary)
    if args.result:
        ending_equity = final.get('equity_at_liquidation_quote_usd')
        ending_cash = D(final['cash_usd'])
        open_cost = D(final.get('open_cost_usd') or 0)
        goal_reached = ((ending_equity is not None and D(ending_equity) >= GOAL)
                        or (ending_cash >= GOAL and open_cost == 0))
        copies = [d for d in session_decisions if d.get('status') in ('PAPER_BUY', 'PAPER_SELL')]
        skips = [d for d in session_decisions if d.get('status') == 'SKIP']
        fees = sum((D(d.get('fee') or 0) for d in copies), ZERO)
        result=dict(name='Paper C',paper_only=True,live_orders=False,private_keys_used=False,brez_used=False,
                    leader_wallet=config['leader_wallet'],leader_handle='@bosona',
                    starting_cash_usd=str(D(config['starting_cash_usd'])),
                    ending_cash_usd=final['cash_usd'],realized_pnl_usd=final['realized_pnl_usd'],
                    fees_usd=str(fees),
                    copies=len(copies),
                    buys_copied=sum(d.get('status')=='PAPER_BUY' for d in copies),
                    sells_copied=sum(d.get('status')=='PAPER_SELL' for d in copies),
                    skips=len(skips),
                    skip_reasons=sorted({d.get('reason') for d in skips if d.get('reason')}),
                    unrealized_pnl_usd=final.get('unrealized_pnl_at_liquidation_quote_usd'),
                    equity_usd=ending_equity,any_sell_copied=any(d.get('status')=='PAPER_SELL' for d in session_decisions),
                    decisions=len(session_decisions),
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
                    reset_to_starting_cash=rule_reset is not None or sample_reset,
                    rule_changed_this_run=rule_reset is not None or sample_reset,
                    rule_changed=(half_rule if sample_reset
                                  else None if rule_reset is None else rule_reset['rule_changed']),
                    rule_change_why=(('Starting cash is $37.40. The one rule change is that one buy cannot spend more than half of current cash, and never more than his size. '
                                      'His price or better, the same-minute sell above paper cost, the 5-share minimum, and the 0.07 crypto taker fee stay. '
                                      'The paper goal is $75. No live order is placed.')
                                     if sample_reset else None if rule_reset is None else rule_reset['why']),
                    second_rule_changed=False,
                    closes=[dict(outcome=d.get('outcome'),slug=d.get('slug'),shares=d.get('shares'),
                                 resolution_price=d.get('resolution_price'),proceeds_usd=d.get('proceeds_usd'),
                                 cost_usd=d.get('cost_usd'),realized_pnl_usd=d.get('realized_pnl_usd'),
                                 cash_usd=d.get('cash_usd'),unrealized_pnl_usd=d.get('unrealized_pnl_usd'),
                                 equity_usd=d.get('equity_usd'),equity_reached_75=d.get('equity_reached_75'),
                                 decision_latency_seconds=d.get('decision_latency_seconds'),
                                 latency_note=d.get('latency_note'),price_source=d.get('price_source'),
                                 reason=d.get('reason'),our_price=d.get('our_price'))
                            for d in session_decisions
                            if d.get('status')=='PAPER_RESOLUTION' or d.get('reason')=='same_minute_bid_above_paper_cost'],
                    book_was_steadily_losing=rule_reset is not None,
                    goal_usd=str(GOAL),goal_reached=goal_reached,poll_seconds=config['poll_seconds'],
                    latency_definition='seconds from his fill timestamp to the paper copy or skip',
                    filter=('One buy spends at most half of current cash and never more than his size. '
                            'Same side and market, only at his price or better. A worse price is skipped and is not copied. '
                            'The market minimum is 5 shares. Crypto taker fee is 0.07. '
                            'The fee is shares times feeRate times price times one minus price, rounded to five decimals. '
                            'On a buy that fee is taken in shares. On a sell it comes out of the USDC proceeds. A resolution has no fee. '
                            'Copy a sell he prints only when it closes an existing paper position above paper cost. '
                            'In the same minute a position opens, sell it when the bid is above paper cost without waiting for a sell he prints.'
                            if config.get('sell_same_minute_if_bid_above_cost') else
                            'One buy spends at most half of current cash and never more than his size. '
                            'Same side and market, only at his price or better. A worse price is skipped. The market minimum is 5 shares. '
                            'Copy a sell only when it closes an existing paper position above paper cost.'
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
