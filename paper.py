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
copy is a taker. Every fill charges shares × 0.07 × price × (1 − price).
On a new buy the fee is taken in shares, so the held count is his size minus
fee divided by price. On a sell the fee comes out of the USDC proceeds. A
resolution is not a fill and has no fee. Makers pay nothing. A schedule that
names another rate does not replace 0.07.
sell_any_minute_if_bid_above_cost sells in any minute when the bid is above
paper cost. A bid at or below cost stays open so the spread is not locked in.
Closed profit is not spent on a new buy unless five shares fill at his price
or at most one tick worse. The opening minute is not
special. Decision latency is his fill timestamp to that copy or skip.
seconds_from_start is measured from this restart until marked cash hits the
goal. A resolution does not count as that goal. The paper48 path is unchanged.
"""
import argparse
import base64
import hashlib
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
from urllib.parse import quote as urlquote

from bot import (activity, array, decimal as D, get_json, market_timeframe, row_keys,
                   source_skip, validate, RateLimited)

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
    """Crypto taker fee on every fill. Makers pay nothing and there is no rebate.

    The charge is shares × 0.07 × price × (1 − price), rounded to five
    decimals. Fees off, or a geopolitics market, charge 0. A schedule that
    names some other rate does not replace 0.07.
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
    return Decimal('0.07')


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

    def goal_amount(self):
        return D(self.config.get('goal_usd') or '75')

    def goal_run_started_at(self, now=None):
        row = self.db.execute("SELECT value FROM meta WHERE key='goal_run_started_at'").fetchone()
        if row:
            return float(row[0])
        started = time.time() if now is None else float(now)
        self.db.execute("INSERT INTO meta VALUES ('goal_run_started_at',?)", (str(started),))
        return started

    def marked_cash_restart_at(self, now=None):
        """Clock for this restart. Later process restarts keep the same instant."""
        row = self.db.execute("SELECT value FROM meta WHERE key='marked_cash_restart_at'").fetchone()
        if row:
            return float(row[0])
        started = time.time() if now is None else float(now)
        self.db.execute("INSERT INTO meta VALUES ('marked_cash_restart_at',?)", (str(started),))
        return started

    def resolution_windfall(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='resolution_windfall_usd'").fetchone()
        return D(row[0]) if row else ZERO

    def note_resolution_windfall(self, realized):
        """Positive resolution profit is not progress toward the fill goal."""
        if realized <= 0:
            return self.resolution_windfall()
        total = self.resolution_windfall() + realized
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('resolution_windfall_usd',?)", (str(total),))
        return total

    def marked_fill_cash(self):
        """Cash from the starting balance and repeatable fills, less resolution profit."""
        return self.cash - self.resolution_windfall()

    def cash_goal_reached(self):
        """Flat marked cash at the goal. One resolution cannot satisfy it."""
        if any(position['shares'] > 0 for position in self.holdings().values()):
            return False
        goal = self.goal_amount()
        return self.cash >= goal and self.marked_fill_cash() >= goal

    def _stamp_fill_clock(self, decision, now):
        decision['fill_latency_seconds'] = decision.get('decision_latency_seconds')
        row = self.db.execute("SELECT value FROM meta WHERE key='marked_cash_restart_at'").fetchone()
        if row is None:
            row = self.db.execute("SELECT value FROM meta WHERE key='goal_run_started_at'").fetchone()
        if row:
            decision['seconds_from_start'] = round(float(now) - float(row[0]), 3)

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
                    # Five shares only. His full size was putting closed profit
                    # into unmarked positions and leaving cash under the start.
                    # The clip must fill at his price or at most one tick worse.
                    if source_shares < max(minimum, FIVE):
                        raise ValueError('below_market_minimum')
                    if FIVE < minimum:
                        raise ValueError('below_market_minimum')
                    try:
                        chosen = quote(book,'BUY',FIVE,rate)
                    except ValueError:
                        raise ValueError('his_size_not_on_the_book')
                    decision['simulated_vwap'] = str(chosen['vwap'])
                    if chosen['vwap'] > source_price + tick:
                        raise ValueError('latency_worse_than_leader_price')
                    if chosen['gross'] > self.cash:
                        raise ValueError('his_size_exceeds_cash')
                    quantity = FIVE
                    decision['copy_size'] = 'five'
                    received = quantity - chosen['share_fee']
                    if received <= 0:
                        raise ValueError('taker_fee_consumed_the_shares')
                    debit = chosen['gross']
                    cash = self.cash-debit
                    opening = position['shares'] == 0
                    position['shares'] += received
                    position['cost'] += debit
                    fill = dict(chosen, shares=received)
                    decision['fee_collected_in'] = 'shares'
                    decision['share_fee'] = str(fill['share_fee'])
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
            if decision['status'] in ('PAPER_BUY','PAPER_SELL'):
                self._stamp_fill_clock(decision, now)
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

    def realize_any_minute_if_bid_above_cost(self, market, book, now):
        """Sell a paper position in any minute when the bid is above paper cost.

        A bid that is no longer above paper cost does not sell. Selling there
        locks in the spread and the taker fee. The book must hold the full
        position. Missing size is not invented.
        """
        if not (self.config.get('strategy')=='paper_c' and self.config.get('sell_any_minute_if_bid_above_cost')):
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
                quantity = position['shares']
                if minimum <= 0 or quantity <= 0:
                    return None
                cost_per = position['cost']/quantity
                removed = position['cost']
                try:
                    at_or_above = quote(book,'SELL',quantity,rate,cost_per)
                except ValueError:
                    at_or_above = None
                if at_or_above is not None and at_or_above['vwap'] > cost_per:
                    fill = at_or_above
                    net = fill['gross']-fill['fee']
                    if net <= removed:
                        return None
                    reason = 'any_minute_bid_above_paper_cost'
                    latency_note = ('The bid was above paper cost. This sell does not wait for the '
                                    'opening minute or for a sell he prints. Latency is his opening fill to this sell.')
                else:
                    try:
                        fill = quote(book,'SELL',quantity,rate)
                    except ValueError:
                        return None
                    # One tick under cost is the spread. A wider drop is sold
                    # so the position does not stay unmarked into a resolution.
                    if fill['vwap'] > cost_per - D(book['tick_size']):
                        return None
                    net = fill['gross']-fill['fee']
                    reason = 'any_minute_bid_no_longer_above_paper_cost'
                    latency_note = ('The bid is more than one tick under paper cost, so the position '
                                    'is sold instead of being held unmarked. Latency is his opening fill to this sell.')
                cash = self.cash+net
                position['shares'] = ZERO
                position['cost'] = ZERO
                self.db.execute("UPDATE meta SET value=? WHERE key='cash'",(str(cash),))
                source_ts = position.get('opened_source_timestamp', position['row'].get('timestamp'))
                latency = None if source_ts is None else round(now-int(source_ts), 3)
                opened = position.get('opened_at')
                decision = dict(status='PAPER_SELL',reason=reason,rule_skipped=False,
                                paper=True,executed=False,side='SELL',slug=position['row'].get('slug'),
                                outcome=position['row'].get('outcome'),token_id=token,
                                shares=str(quantity),gross=str(fill['gross']),fee=str(fill['fee']),
                                vwap=str(fill['vwap']),our_price=str(fill['vwap']),his_price=None,cent_gap=None,
                                realized_pnl_usd=str(net-removed),cash_usd=str(cash),held_shares='0',
                                cost_per_share_usd=str(cost_per),cost_usd=str(removed),
                                source_timestamp_seconds=source_ts,decision_latency_seconds=latency,
                                latency_note=latency_note)
                self._stamp_fill_clock(decision, now)
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?)',
                                (token,json.dumps(position,default=str)))
                held_after = sum((p['shares'] for p in self.holdings().values()), ZERO)
                if held_after == 0:
                    decision['unrealized_pnl_usd'] = '0'
                    decision['equity_usd'] = str(cash)
                    decision['goal_usd'] = str(self.goal_amount())
                    decision['marked_fill_cash_usd'] = str(self.marked_fill_cash())
                    decision['equity_reached_goal'] = self.cash_goal_reached()
                seen = f"any-minute:{token}:{opened if opened is not None else 'open'}"
                self.db.execute('INSERT INTO seen VALUES (?,?)', (seen, json.dumps(decision)))
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
                self.note_resolution_windfall(realized)
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
                    decision['goal_usd'] = str(self.goal_amount())
                    decision['marked_fill_cash_usd'] = str(self.marked_fill_cash())
                    # A resolution can raise cash. It is not the fill goal.
                    decision['equity_reached_goal'] = False
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


def trade_timestamp_seconds(value):
    """Activity timestamps are seconds. The public stream often sends milliseconds."""
    stamp = int(D(str(value)))
    if stamp > 10**14:
        return stamp // 1_000_000
    if stamp > 10**11:
        return stamp // 1000
    return stamp


def trade_fingerprint(row):
    """Stable id for one fill from either the REST feed or the public trade stream.

    The two feeds can stamp the same fill one second apart. The transaction,
    token, side, size, and price identify it. The second is not part of the id.
    """
    size = D(str(row.get('size'))).quantize(Decimal('0.00000001'))
    price = D(str(row.get('price'))).quantize(Decimal('0.000001'))
    wallet = row.get('proxy_wallet') or row.get('proxyWallet') or ''
    transaction = row.get('transaction_hash') or row.get('transactionHash') or ''
    condition = row.get('condition_id') or row.get('conditionId') or ''
    token = row.get('token_id') or row.get('asset') or ''
    identity = '|'.join([
        str(wallet).lower(), str(transaction).lower(), str(condition).lower(), str(token),
        str(row.get('side') or '').upper(), format(size, 'f'), format(price, 'f'),
    ])
    return hashlib.sha256(identity.encode()).hexdigest()


def live_trade_row(payload):
    """Public activity-stream trade mapped onto the REST row shape. No order is sent."""
    return dict(
        proxy_wallet=payload.get('proxyWallet'),
        transaction_hash=payload.get('transactionHash'),
        condition_id=payload.get('conditionId'),
        token_id=str(payload.get('asset')),
        timestamp=trade_timestamp_seconds(payload.get('timestamp')),
        side=payload.get('side'),
        size=payload.get('size'),
        price=payload.get('price'),
        type='TRADE',
        slug=payload.get('slug'),
        outcome=payload.get('outcome'),
        is_combo=False,
    )


def decode_ws_frames(buffer):
    """Split a byte buffer into complete websocket frames. Returns (frames, rest)."""
    frames = []
    while len(buffer) >= 2:
        first, second = buffer[0], buffer[1]
        masked = (second & 0x80) != 0
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < 4:
                break
            length = struct.unpack('!H', buffer[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buffer) < 10:
                break
            length = struct.unpack('!Q', buffer[2:10])[0]
            offset = 10
        mask = b''
        if masked:
            if len(buffer) < offset + 4 + length:
                break
            mask = buffer[offset:offset + 4]
            offset += 4
        elif len(buffer) < offset + length:
            break
        payload = buffer[offset:offset + length]
        buffer = buffer[offset + length:]
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        frames.append(((first & 0x80) != 0, first & 0x0F, payload))
    return frames, buffer


def _ws_frame(sock, opcode, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack('!H', length)
    else:
        header.append(0x80 | 127)
        header += struct.pack('!Q', length)
    header += mask
    header += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header)


def _ws_send(sock, text):
    _ws_frame(sock, 1, text)


def stream_wallet_trades(wallet, outbox, stop, alive):
    """Push his public trades as soon as the activity stream prints them.

    This only reads a public websocket. It does not sign or send an order.
    """
    host = 'ws-live-data.polymarket.com'
    wallet = wallet.lower()
    while not stop.is_set():
        sock = None
        try:
            raw = socket.create_connection((host, 443), timeout=10)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                f'GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
            ).encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError('websocket_handshake_closed')
                buf += chunk
            head, buf = buf.split(b'\r\n\r\n', 1)
            status = head.split(b'\r\n', 1)[0]
            if b' 101 ' not in status:
                raise ConnectionError('websocket_handshake_rejected')
            _ws_send(sock, json.dumps({
                'action': 'subscribe',
                'subscriptions': [{'topic': 'activity', 'type': 'trades'}],
            }))
            sock.settimeout(1)
            last_ping = time.monotonic()
            text_parts = []
            text_opcode = None
            while not stop.is_set():
                if time.monotonic() - last_ping > 4:
                    _ws_send(sock, 'PING')
                    last_ping = time.monotonic()
                try:
                    chunk = sock.recv(1 << 16)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                frames, buf = decode_ws_frames(buf)
                for fin, opcode, payload in frames:
                    if opcode == 8:
                        raise ConnectionError('websocket_closed')
                    if opcode == 9:
                        _ws_frame(sock, 0xA, payload)
                        continue
                    if opcode in (1, 2):
                        text_parts = [payload]
                        text_opcode = opcode
                    elif opcode == 0 and text_parts:
                        text_parts.append(payload)
                    else:
                        continue
                    if not fin or text_opcode != 1:
                        continue
                    text = b''.join(text_parts).decode('utf-8', 'replace')
                    text_parts = []
                    alive['at'] = time.time()
                    if text in ('PING', 'PONG'):
                        continue
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    payload = message.get('payload') if isinstance(message, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if str(payload.get('proxyWallet') or '').lower() != wallet:
                        continue
                    if market_timeframe(payload.get('slug')) is None:
                        continue
                    outbox.put(live_trade_row(payload))
        except Exception:
            if not stop.is_set():
                time.sleep(0.5)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


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
                fill_latency_seconds=decision.get('fill_latency_seconds'),
                seconds_from_start=decision.get('seconds_from_start'),
                fee_usd=decision.get('fee'),fee_collected_in=decision.get('fee_collected_in'),
                share_fee=decision.get('share_fee'),
                copied_sell=decision.get('status')=='PAPER_SELL',rule_skipped=decision.get('rule_skipped'),
                price_note=decision.get('price_note'))


def run_paper_c(args, config, journal, observer_start, source_start):
    """Keep one paper book running until equity reaches the goal.

    A stopped process is resumed on this same book by the caller. No live order
    is sent. His trades are read from the public stream as they print, and the
    REST feed is only the backup.
    """
    run_started_wall = time.time()
    journal.goal_run_started_at(run_started_wall)
    goal_started = journal.marked_cash_restart_at(run_started_wall)
    journal.db.commit()
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
            mark_goal(decision)

        def note_bid_sell(exited, window_index):
            if not exited:
                return
            goal = journal.goal_amount()
            if exited.get('unrealized_pnl_usd') is None:
                port = journal.portfolio()
                if not port.get('unresolved_positions'):
                    exited['unrealized_pnl_usd'] = port.get('unrealized_pnl_at_liquidation_quote_usd')
                    exited['equity_usd'] = port.get('equity_at_liquidation_quote_usd')
                    equity = port.get('equity_at_liquidation_quote_usd')
                    exited['goal_usd'] = str(goal)
                    exited['equity_reached_goal'] = equity is not None and D(equity) >= goal
            emit(exited)
            remember(window_index, exited)

        def position_open():
            return any(position['shares'] > 0 for position in journal.holdings().values())

        def sell_if_bid_above_cost(window_index, market=None, book=None):
            if not config.get('sell_any_minute_if_bid_above_cost'):
                return
            books = []
            if market is not None and book is not None:
                books.append((market, book))
            else:
                for token, position in list(journal.holdings().items()):
                    if position['shares'] <= 0:
                        continue
                    try:
                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        books.append((market_future.result(), book_future.result()))
                    except RateLimited:
                        raise
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc),token_id=token))
            for market_row, book_row in books:
                try:
                    note_bid_sell(
                        journal.realize_any_minute_if_bid_above_cost(market_row, book_row, time.time()),
                        window_index)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))

        rule_name = 'sell_any_minute_if_bid_above_cost' if config.get('sell_any_minute_if_bid_above_cost') else None
        buy_filter = ('copy his exact share count, same side and market, only when the full size fills at his price or better and the debit does not spend closed profit; '
                      'closed profit may buy five shares only when those five fill at his price or better; '
                      'a full size whose ask has walked may buy five shares at his price or at most one tick worse; '
                      'skip when that size is not on the book; do not copy at a worse price; '
                      'a new buy pays the taker fee in shares and a new sell pays it from USDC proceeds; '
                      'copy a sell he prints only when it closes an existing paper position above paper cost')
        if rule_name:
            buy_filter += '; in any minute, sell when the bid is above paper cost, and sell when the bid is no longer above paper cost'
        emit(dict(status='STARTED',strategy='paper_c',paper=True,executed=False,live_orders=False,
                  leader_wallet=config['leader_wallet'],starting_cash_usd=str(config['starting_cash_usd']),
                  resumed_cash_usd=str(journal.cash),goal_usd=str(journal.goal_amount()),
                  rule_changed_this_run=rule_name is not None,
                  reset_this_run=False,
                  rule_changed=rule_name,
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
                  goal_usd=str(journal.goal_amount()),
                  equity_reached_goal=opening_equity>=journal.goal_amount()))
        trades = queue.Queue()
        stop_feed = threading.Event()
        feed_alive = {'at': 0.0}
        feed = threading.Thread(
            target=stream_wallet_trades,
            args=(config['leader_wallet'], trades, stop_feed, feed_alive),
            daemon=True)
        feed.start()
        goal_hit = {'done': False}

        def mark_goal(decision):
            if decision.get('status') == 'PAPER_RESOLUTION':
                return
            if journal.cash_goal_reached():
                goal_hit['done'] = True
                emit(dict(status='GOAL', paper=True, executed=False, live_orders=False,
                          goal_usd=str(journal.goal_amount()), cash_usd=str(journal.cash),
                          marked_fill_cash_usd=str(journal.marked_fill_cash()),
                          equity_usd=str(journal.cash),
                          seconds_from_start=round(time.time()-goal_started, 3),
                          marked_cash_restart_at=goal_started))

        try:
            window_index = 1
            while not goal_hit['done']:
                window_started = time.monotonic()
                window_decisions = []
                emit(dict(status='WINDOW_START',window=window_index,cash_usd=str(journal.cash),
                          paper=True,executed=False))
                while time.monotonic()-window_started < args.duration and not goal_hit['done']:
                    next_request = time.monotonic()+config['poll_seconds']
                    limited = False
                    # An open position sells on the first book whose bid clears
                    # cost, in any minute. The activity request must not hold that check.
                    try:
                        queued=[]
                        while True:
                            try:
                                queued.append(trades.get_nowait())
                            except queue.Empty:
                                break
                        # The public stream is the real-time path. REST is only
                        # the backup, and only while that stream is quiet, so a
                        # healthy socket does not add another poller.
                        stream_fresh = time.time() - feed_alive.get('at', 0) < 2
                        if queued:
                            rows=queued
                        elif stream_fresh:
                            rows=[]
                            if position_open():
                                sell_if_bid_above_cost(window_index)
                        else:
                            if position_open():
                                sell_if_bid_above_cost(window_index)
                            activity_future=pool.submit(activity,config['leader_wallet'],max(source_start,int(time.time())-120),int(time.time()))
                            while not activity_future.done() and position_open() and time.monotonic()-window_started < args.duration and not goal_hit['done']:
                                watched=time.monotonic()
                                sell_if_bid_above_cost(window_index)
                                pause=config['poll_seconds']-(time.monotonic()-watched)
                                if pause > 0 and not activity_future.done():
                                    time.sleep(pause)
                            rows=activity_future.result()
                        for key,row in ((trade_fingerprint(row), row) for row in rows):
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
                                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':row['token_id']}, True)
                                        market,book=market_future.result(),book_future.result()
                                        acted=time.time()
                                        decision=journal.process(key,row,market,book,acted)
                                        emit(decision)
                                        remember(window_index, decision)
                                        if decision.get('status')=='PAPER_BUY':
                                            sell_if_bid_above_cost(window_index, market, book)
                                            sell_if_bid_above_cost(window_index)
                                    else:
                                        reason=('paper_c_no_new_buys' if row['side']=='BUY'
                                                else 'no_matching_paper_position')
                                        decision=journal.process(key,row,{},{},appeared,skip_reason=reason)
                                        emit(decision)
                                        remember(window_index, decision)
                            except RateLimited:
                                raise
                            except Exception as exc:
                                emit(dict(status='ERROR',message=str(exc),event_id=key))
                    except RateLimited as exc:
                        limited = True
                        if exc.fresh:
                            emit(dict(status='BACKOFF',paper=True,executed=False,host=exc.host,
                                      retry_after_seconds=exc.retry_after,message='rate_limited'))
                        next_request=max(next_request,time.monotonic()+exc.retry_after)
                    except Exception as exc:
                        emit(dict(status='ERROR',message=str(exc)))
                        next_request=max(next_request,time.monotonic()+0.5)
                    if not limited:
                        sell_if_bid_above_cost(window_index)
                    remaining=args.duration-(time.monotonic()-window_started)
                    if remaining > 0 and (limited or not position_open()):
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
                for close in journal.realize_public_resolutions(get_json, time.time()):
                    emit(close)
                    if close.get('status')=='PAPER_RESOLUTION':
                        session_decisions.append(close)
                        window_decisions.append(decision_view(close, window_index))
                        mark_goal(close)
                port = journal.portfolio()
                equity = None if port.get('unresolved_positions') else D(port.get('equity_at_liquidation_quote_usd',port['cash_usd']))
                if equity is not None:
                    equity_samples.append(equity)
                    if journal.cash_goal_reached() and not goal_hit['done']:
                        goal_hit['done'] = True
                        emit(dict(status='GOAL',paper=True,executed=False,live_orders=False,
                                  goal_usd=str(journal.goal_amount()),cash_usd=str(journal.cash),
                                  marked_fill_cash_usd=str(journal.marked_fill_cash()),
                                  equity_usd=str(journal.cash),
                                  seconds_from_start=round(time.time()-goal_started,3),
                                  marked_cash_restart_at=goal_started))
                window_record = dict(status='WINDOW',window=window_index,trades=len(window_decisions),
                                     note=None if window_decisions else 'No trade in this window.',
                                     decisions=window_decisions,cash_usd=port['cash_usd'],
                                     realized_pnl_usd=port['realized_pnl_usd'],
                                     unrealized_pnl_usd=port.get('unrealized_pnl_at_liquidation_quote_usd'),
                                     equity_usd=None if equity is None else str(equity),
                                     open_cost_usd=port['open_cost_usd'],paper=True,executed=False,
                                     seconds_from_start=round(time.time()-goal_started,3))
                windows.append(window_record)
                emit(window_record)
                window_index += 1
        except KeyboardInterrupt:
            pass
        finally:
            stop_feed.set()
        final = journal.portfolio()
        emit(final)
        latencies=[d['decision_latency_seconds'] for d in session_decisions
                   if isinstance(d.get('decision_latency_seconds'),(int,float)) and d['decision_latency_seconds']>=0]
        copies = sum(d.get('status') in ('PAPER_BUY','PAPER_SELL') for d in session_decisions)
        skips = sum(d.get('status')=='SKIP' for d in session_decisions)
        buy_latencies=sorted(d['fill_latency_seconds'] for d in session_decisions
                             if d.get('status')=='PAPER_BUY' and isinstance(d.get('fill_latency_seconds'),(int,float)))
        if not buy_latencies:
            median_buy_latency=None
        elif len(buy_latencies)%2==1:
            median_buy_latency=buy_latencies[len(buy_latencies)//2]
        else:
            mid=len(buy_latencies)//2
            median_buy_latency=(buy_latencies[mid-1]+buy_latencies[mid])/2
        summary=dict(status='SUMMARY',duration_seconds=round(time.monotonic()-started,2),
                     windows=len(windows),decisions=len(session_decisions),
                     copies=copies,skips=skips,
                     buy_count=len(buy_latencies),
                     sells_copied=sum(d.get('status')=='PAPER_SELL' for d in session_decisions),
                     fees_usd=final.get('total_simulated_fees_usd'),
                     cash_usd=final.get('cash_usd'),realized_pnl_usd=final.get('realized_pnl_usd'),
                     open_cost_usd=final.get('open_cost_usd'),
                     marked_fill_cash_usd=str(journal.marked_fill_cash()),
                     median_buy_latency_seconds=median_buy_latency,
                     decision_latency_min_seconds=min(latencies) if latencies else None,
                     decision_latency_max_seconds=max(latencies) if latencies else None,
                     reset_to_starting_cash=rule_reset is not None,
                     rule_changed=rule_name if rule_reset is None else rule_reset['rule_changed'],
                     second_rule_changed=False,
                     seconds_from_start=round(time.time()-goal_started,3),
                     goal_usd=str(journal.goal_amount()),
                     goal_reached=goal_hit['done'],
                     note='Paper fills only. No live orders. Latency is his fill timestamp to the copy or skip. seconds_from_start is measured from this restart until marked cash from fills hits the goal. A resolution does not count.')
        emit(summary)
    if args.result:
        ending_equity = final.get('equity_at_liquidation_quote_usd')
        goal = journal.goal_amount()
        goal_reached = journal.cash_goal_reached()
        result=dict(name='Paper C',paper_only=True,live_orders=False,private_keys_used=False,brez_used=False,
                    leader_wallet=config['leader_wallet'],leader_handle='@bosona',
                    starting_cash_usd=str(D(config['starting_cash_usd'])),
                    ending_cash_usd=final['cash_usd'],realized_pnl_usd=final['realized_pnl_usd'],
                    fees_usd=final.get('total_simulated_fees_usd'),
                    copies=copies,skips=skips,
                    unrealized_pnl_usd=final.get('unrealized_pnl_at_liquidation_quote_usd'),
                    equity_usd=ending_equity,any_sell_copied=any(d.get('status')=='PAPER_SELL' for d in session_decisions),
                    sells_copied=sum(d.get('status')=='PAPER_SELL' for d in session_decisions),
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
                                                   latency_seconds=view['decision_latency_seconds'],
                                                   fill_latency_seconds=view.get('fill_latency_seconds'),
                                                   seconds_from_start=view.get('seconds_from_start'))
                                              for window in windows for view in window['decisions']],
                    reset_to_39=False,
                    rule_changed_this_run=rule_name is not None or rule_reset is not None,
                    rule_changed=rule_name if rule_reset is None else rule_reset['rule_changed'],
                    rule_change_why=(rule_reset['why'] if rule_reset is not None else
                                     ('The one rule change is to sell a paper position in any minute when the bid is above paper cost, '
                                      'not only in the minute it opened. Starting cash is $37.40. The goal is $75. '
                                      'Exact size, his price or better, the 5-share minimum, and the 0.07 crypto taker fee stay. '
                                      'A worse price is not copied. No live order is placed.')
                                     if rule_name else None),
                    second_rule_changed=False,
                    closes=[dict(outcome=d.get('outcome'),slug=d.get('slug'),shares=d.get('shares'),
                                 resolution_price=d.get('resolution_price'),proceeds_usd=d.get('proceeds_usd'),
                                 cost_usd=d.get('cost_usd'),realized_pnl_usd=d.get('realized_pnl_usd'),
                                 cash_usd=d.get('cash_usd'),unrealized_pnl_usd=d.get('unrealized_pnl_usd'),
                                 equity_usd=d.get('equity_usd'),equity_reached_goal=d.get('equity_reached_goal'),
                                 goal_usd=d.get('goal_usd'),
                                 decision_latency_seconds=d.get('decision_latency_seconds'),
                                 latency_note=d.get('latency_note'),price_source=d.get('price_source'),
                                 reason=d.get('reason'),our_price=d.get('our_price'),fee=d.get('fee'))
                            for d in session_decisions
                            if d.get('status')=='PAPER_RESOLUTION' or d.get('reason')=='any_minute_bid_above_paper_cost'],
                    book_was_steadily_losing=rule_reset is not None,
                    goal_usd=str(goal),goal_reached=goal_reached,poll_seconds=config['poll_seconds'],
                    latency_definition='seconds from his fill timestamp to the paper copy or skip',
                    filter=('Copy his exact share count, same side and market, only when the full size fills at his price or better. '
                            'Skip that buy when the size is not on the book or costs more than the cash on hand; do not scale it down. The market minimum is 5 shares. '
                            'Do not copy at a worse price. '
                            'A new fill is a taker. The fee is shares times feeRate times price times one minus price, rounded to five decimals. '
                            'Every fill uses the real crypto taker fee, shares times 0.07 times price times one minus price. A schedule that names another rate does not replace 0.07. '
                            'On a buy that fee is taken in shares. On a sell it comes out of the USDC proceeds. A resolution has no fee. '
                            'Copy a sell he prints only when it closes an existing paper position above paper cost. '
                            'In any minute, sell the position when the bid is above paper cost without waiting for a sell he prints.'
                            if config.get('sell_any_minute_if_bid_above_cost') else
                            'Copy his exact share count, same side and market, only when the book fills at his price or better. '
                            'Skip that buy when the size costs more than the cash on hand; do not scale it down. The market minimum is 5 shares. '
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
                limited = False
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
                except RateLimited as exc:
                    limited = True
                    if exc.fresh:
                        emit(dict(status='BACKOFF',paper=True,executed=False,host=exc.host,
                                  retry_after_seconds=exc.retry_after,message='rate_limited'))
                    next_request=max(next_request,time.monotonic()+exc.retry_after)
                except Exception as exc:
                    emit(dict(status='ERROR',message=str(exc)))
                    next_request=max(next_request,time.monotonic()+2)
                if limited:
                    remaining=args.duration-(time.monotonic()-started)
                    if remaining > 0:
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
                    continue
                for token,position in list(journal.holdings().items()):
                    if position['shares'] <= 0:
                        continue
                    try:
                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(position['row']['slug'],safe=''))
                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':token})
                        exited=journal.realize_if_bid_above_cost(market_future.result(),book_future.result(),time.time())
                        if exited:
                            emit(exited)
                    except RateLimited as exc:
                        if exc.fresh:
                            emit(dict(status='BACKOFF',paper=True,executed=False,host=exc.host,
                                      retry_after_seconds=exc.retry_after,message='rate_limited'))
                        next_request=max(next_request,time.monotonic()+exc.retry_after)
                        break
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
