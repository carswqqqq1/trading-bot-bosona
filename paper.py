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

strategy "paper_c" is a separate filter. It never opens a buy. It copies a
sell only when that sell closes an existing paper position above paper cost.
A trade with no matching paper position is skipped at the moment it appears.
Decision latency is his fill timestamp to that copy or skip. The paper48 path
is unchanged.
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
                # Paper C can refuse a buy, or a sell with nothing open, before any book read.
                if self.config.get('strategy')=='paper_c' and (row['side']=='BUY' or position['shares']<=0):
                    raise ValueError('paper_c_no_new_buys' if row['side']=='BUY' else 'no_matching_paper_position')
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
                if self.config.get('strategy')=='paper_c':
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
                if self.config.get('strategy')!='paper_c':
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
                # Positive means our price is above his fill. Same side and market.
                decision['cent_difference'] = str((D(our_price)-D(decision['source_price']))*100)
                decision['cent_gap'] = decision['cent_difference']
            elif decision.get('status')=='SKIP':
                decision['our_price'] = None
                decision['cent_gap'] = None
            if self.config.get('strategy')=='paper_c' and decision.get('status')=='SKIP':
                # A skip is not a fill. Leave his price, and do not invent ours.
                decision['our_price'] = None
                decision['cent_gap'] = None
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
                copied_sell=decision.get('status')=='PAPER_SELL',rule_skipped=decision.get('rule_skipped'),
                price_note=decision.get('price_note'))


def run_paper_c(args, config, journal, observer_start, source_start):
    """Several short live windows. Decide each new trade before the next poll wait."""
    run_started_wall = time.time()
    observations, session_decisions, windows, equity_samples = {}, [], [], []
    rule_reset = None
    started = time.monotonic()
    with Path(args.output).open('a') as output, ThreadPoolExecutor(max_workers=2) as pool:
        def emit(record):
            line=json.dumps(record)
            output.write(line+'\n');output.flush();print(line,flush=True)

        def remember(window_index, decision):
            if decision.get('status')=='DUPLICATE' or decision.get('reason')=='resume_backlog_not_copied':
                return
            session_decisions.append(decision)
            window_decisions.append(decision_view(decision, window_index))

        opening = journal.portfolio()
        opening_equity = D(opening.get('equity_at_liquidation_quote_usd', opening['cash_usd']))
        equity_samples.append(opening_equity)
        emit(dict(status='STARTED',strategy='paper_c',paper=True,executed=False,live_orders=False,
                  leader_wallet=config['leader_wallet'],starting_cash_usd=str(config['starting_cash_usd']),
                  cash_usd=str(journal.cash),equity_usd=str(opening_equity),
                  observer_start_utc=datetime.fromtimestamp(observer_start,timezone.utc).isoformat(),
                  run_started_at_utc=datetime.fromtimestamp(run_started_wall,timezone.utc).isoformat(),
                  poll_seconds=config['poll_seconds'],windows=args.windows,window_seconds=args.duration,
                  latency='seconds from his fill timestamp to the paper copy or skip',
                  filter='no new buys; copy a sell only when it closes an existing paper position above paper cost'))
        try:
            for window_index in range(1, args.windows+1):
                window_started = time.monotonic()
                window_decisions = []
                emit(dict(status='WINDOW_START',window=window_index,cash_usd=str(journal.cash),
                          paper=True,executed=False))
                while time.monotonic()-window_started < args.duration:
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
                                else:
                                    held = journal.holdings().get(str(row['token_id']))
                                    needs_book = row['side']=='SELL' and held and held['shares']>0
                                    if needs_book:
                                        market_future=pool.submit(get_json,'https://gamma-api.polymarket.com/markets/slug/'+urlquote(row['slug'],safe=''))
                                        book_future=pool.submit(get_json,'https://clob.polymarket.com/book',{'token_id':row['token_id']})
                                        market,book=market_future.result(),book_future.result()
                                        acted=time.time()
                                        decision=journal.process(key,row,market,book,acted)
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
                    remaining=args.duration-(time.monotonic()-window_started)
                    if remaining > 0:
                        time.sleep(min(max(0,next_request-time.monotonic()),remaining))
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
                if rule_reset is None and steadily_losing(equity_samples):
                    reset = journal.apply_one_rule_reset()
                    reset.update(window_after=window_index,equity_samples_usd=[str(sample) for sample in equity_samples])
                    rule_reset = reset
                    emit(reset)
                    equity_samples=[D(journal.cash)]
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
                     reset_to_starting_cash=rule_reset is not None,
                     rule_changed=None if rule_reset is None else rule_reset['rule_changed'],
                     note='Paper fills only. No live orders. Latency is his fill timestamp to the copy or skip.')
        emit(summary)
    if args.result:
        ending_equity = final.get('equity_at_liquidation_quote_usd')
        goal_reached = ending_equity is not None and D(ending_equity) >= D('78')
        result=dict(name='Paper C',paper_only=True,live_orders=False,private_keys_used=False,brez_used=False,
                    leader_wallet=config['leader_wallet'],leader_handle='@bosona',
                    starting_cash_usd=str(D(config['starting_cash_usd'])),
                    ending_cash_usd=final['cash_usd'],realized_pnl_usd=final['realized_pnl_usd'],
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
                                                   latency_seconds=view['decision_latency_seconds'])
                                              for window in windows for view in window['decisions']],
                    reset_to_39=rule_reset is not None,
                    rule_changed=None if rule_reset is None else rule_reset['rule_changed'],
                    rule_change_why=None if rule_reset is None else rule_reset['why'],
                    book_was_steadily_losing=rule_reset is not None,
                    goal_usd='78',goal_reached=goal_reached,poll_seconds=config['poll_seconds'],
                    latency_definition='seconds from his fill timestamp to the paper copy or skip',
                    filter='Do not open new buys. Copy a sell only when it closes an existing paper position above paper cost. Skip when there is no matching paper position.',
                    windows=[dict(window=window['window'],trades=window['trades'],note=window['note'],
                                  his_trades=window['decisions'],cash_usd=window['cash_usd'],
                                  realized_pnl_usd=window['realized_pnl_usd'],
                                  unrealized_pnl_usd=window['unrealized_pnl_usd'])
                             for window in windows])
        Path(args.result).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(dict(status='RESULT_WRITTEN',path=args.result),flush=True))


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
