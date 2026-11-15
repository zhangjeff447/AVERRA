"""Paper order sizing from reconciled balances and contemporaneous quotes."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .policy import CORE, SYMBOLS, D


CENT = D('.01')
SHARE = D('.000001')
CORE_CASH = 'external_core'
TACTICAL_CASH = 'tactical'


def down(amount, step=CENT):
    return max(D(0), amount).quantize(step, rounding=ROUND_DOWN)


def authorize(symbol, side):
    if symbol not in SYMBOLS or side not in ('BUY','SELL') or (symbol in CORE and side == 'SELL'):
        raise ValueError('paper_order_not_authorized')


@dataclass(frozen=True)
class Frame:
    session: int
    day: str
    reference_day: str
    execution_at: datetime
    quoted_at: datetime
    prices: dict[str, Decimal]
    tqqq_prior_close: Decimal | None = None

    def validate(self, policy):
        from .data import ET, preceding, session_times
        if (self.execution_at.tzinfo is None or self.quoted_at.tzinfo is None
                or not 0 <= (self.execution_at-self.quoted_at).total_seconds() <= policy['execution']['max_quote_age_seconds']
                or set(self.prices) != set(SYMBOLS)
                or any(not price.is_finite() or price <= 0 for price in self.prices.values())):
            raise ValueError('quotes_unverified')
        opened,closed=session_times(self.day)
        if (self.reference_day!=preceding(self.day) or not opened<=self.execution_at<=closed
                or self.execution_at.astimezone(ET).date().isoformat()!=self.day):
            raise ValueError('execution_before_signal')


@dataclass(frozen=True)
class Snapshot:
    frame: Frame
    nav: Decimal
    available: Decimal
    core_cash: Decimal
    tactical_cash: Decimal
    quantities: dict[str, Decimal]
    account_dd: Decimal
    tactical_dd: Decimal

    @property
    def session(self):
        return self.frame.session

    @property
    def prices(self):
        return self.frame.prices

    @property
    def tqqq_prior_close(self):
        return self.frame.tqqq_prior_close


@dataclass(frozen=True)
class Costs:
    slippage_bps: D = D(5)
    commission: D = D(0)
    short_tax: D = D(0)
    long_tax: D = D(0)
    dividend_tax: D = D(0)
    cash_yield_multiplier: D = D(1)

    def price(self, mark, side):
        return mark * (1 + (1 if side == 'BUY' else -1) * self.slippage_bps / 10000)


@dataclass(frozen=True)
class Order:
    key: str
    symbol: str
    side: str
    quantity: Decimal
    fund: str
    reason: str
    cap: Decimal | None = None

    def __post_init__(self):
        authorize(self.symbol,self.side)
        if (not self.key or not self.quantity.is_finite() or self.quantity <= 0
                or self.quantity != down(self.quantity,SHARE)):
            raise ValueError('invalid_order_quantity')
        if self.fund != (CORE_CASH if self.symbol in CORE else TACTICAL_CASH):
            raise ValueError('invalid_order_funding_source')


def core_envelope(snap, weights, floor, policy, *, state=None, daily_remaining=None):
    spendable = min(snap.core_cash, snap.available-D(policy['execution']['cash_buffer']))
    budget = down(spendable if daily_remaining is None else min(spendable, daily_remaining))
    available = {s:snap.quantities[s]*snap.prices[s] for s in CORE}
    core = sum(available.values())
    first = down(budget*D(weights[CORE[0]]))
    amounts = {CORE[0]:first,CORE[1]:budget-first}
    for symbol in CORE:
        if state == 'DEFENSIVE' and symbol == 'SMH' and policy.get('defensive_skip_smh_floor'):
            continue
        if core and available[symbol]/core < D(floor[symbol]):
            target=D(policy['strategic_core_weights'][symbol])
            needed=max(D(0),(core+budget)*target-available[symbol]).quantize(CENT,rounding=ROUND_UP)
            amounts[symbol]=min(budget,max(amounts[symbol],needed))
            other=next(s for s in CORE if s != symbol)
            amounts[other]=budget-amounts[symbol]
    return amounts


def core_order(symbol, budget, snap, costs, policy, reason='CORE_NEW_DEPOSIT'):
    authorize(symbol,'BUY')
    available = min(budget,snap.core_cash,snap.available-D(policy['execution']['cash_buffer']))
    price = costs.price(snap.prices[symbol],'BUY')
    qty = down((available-costs.commission)/price,SHARE)
    if qty*price < D(policy['execution']['minimum_notional']):
        return None
    return Order(snap.frame.day+':CORE:'+symbol,symbol,'BUY',qty,CORE_CASH,reason)


def tactical_order(signal, snap, limits, costs, policy):
    if signal.day != snap.frame.reference_day:
        raise ValueError('signal_not_from_previous_completed_session')
    qty,mark=snap.quantities['TQQQ'],snap.prices['TQQQ']
    held=qty*mark
    sell_price=costs.price(mark,'SELL')
    # The proceeds can only shrink NAV by slippage, fee and tax. Use
    # zero cost basis as the conservative bound for cap-reduction sizing.
    cap_gap=held-limits.cap*(snap.nav-costs.commission)
    sale= D(0)
    if qty and limits.liquidation:
        sale=qty
    elif qty:
        if held > limits.cap*snap.nav:
            cost_per_share=mark-sell_price+max(costs.short_tax,costs.long_tax)*sell_price
            sale=max(sale,cap_gap/(mark-limits.cap*cost_per_share))
        if limits.half_limit is not None:
            sale=max(sale,qty-limits.half_limit)
    if sale>0:
        sale=min(down(qty,SHARE),sale.quantize(SHARE,rounding=ROUND_UP))
        if sale<=0:
            return None
        return Order(snap.frame.day+':TQQQ:SELL','TQQQ','SELL',sale,TACTICAL_CASH,
                     limits.flags[0] if limits.flags else 'TQQQ_CAP',limits.cap)
    if not limits.allow_buy or snap.nav<=0:
        return None
    price=costs.price(mark,'BUY')
    cash=min(snap.tactical_cash,snap.available-D(policy['execution']['cash_buffer']))
    step_cap=min(limits.target,held/snap.nav+limits.max_add_nav)
    gap=(step_cap*(snap.nav-costs.commission)-held)/(mark+step_cap*(price-mark))
    buy=down(min((cash-costs.commission)/price,gap),SHARE)
    if buy*price < D(policy['execution']['minimum_notional']):
        return None
    return Order(snap.frame.day+':TQQQ:BUY','TQQQ','BUY',buy,TACTICAL_CASH,'TQQQ_TARGET',step_cap)
