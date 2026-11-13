"""A credential-free paper ledger. Holdings change only on fills or splits."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date
import json
import sqlite3

from .planner import Order, authorize, CORE_CASH, TACTICAL_CASH, SHARE, down
from .policy import CORE, D, SYMBOLS
from .risk import Drawdown, RiskState, assess


TACTICAL_POCKETS = ('tactical_seed','tqqq_sale','tqqq_dividend','tactical_interest')
POCKETS = (CORE_CASH,*TACTICAL_POCKETS,'core_dividend','core_interest')


@dataclass
class Wallet:
    holdings: dict = field(default_factory=lambda:dict.fromkeys(SYMBOLS,D(0)))
    pockets: dict = field(default_factory=lambda:dict.fromkeys(POCKETS,D(0)))
    pending: list = field(default_factory=list)
    lots: list = field(default_factory=list)
    risk: RiskState = field(default_factory=RiskState)
    account: Drawdown = field(default_factory=Drawdown)
    tactical: Drawdown = field(default_factory=Drawdown)
    flow: D = D(0)
    tactical_flow: D = D(0)
    deposits: D = D(0)
    distributions: D = D(0)
    sale_proceeds: D = D(0)
    realized_gain: D = D(0)
    interest_income: D = D(0)
    taxes: D = D(0)
    commissions: D = D(0)
    volume: D = D(0)
    trades: int = 0

    def deposit(self, amount, tactical=D(0)):
        if not 0 <= tactical <= amount or amount <= 0:
            raise ValueError('invalid_deposit')
        self.pockets[CORE_CASH]+=amount-tactical
        self.pockets['tactical_seed']+=tactical
        self.flow+=amount
        self.tactical_flow+=tactical
        self.deposits+=amount

    def split(self,symbol,ratio):
        if symbol not in SYMBOLS or not ratio.is_finite() or ratio<=0:
            raise ValueError('invalid_split')
        if symbol=='TQQQ':
            self.risk.highest_close/=ratio
            if self.risk.half_limit is not None:
                self.risk.half_limit*=ratio
            self.lots=[(day,shares*ratio,basis) for day,shares,basis in self.lots]
        self.holdings[symbol]*=ratio

    def dividend(self,symbol,per_share,due,costs):
        if symbol not in SYMBOLS or not per_share.is_finite() or per_share<0:
            raise ValueError('invalid_dividend')
        gross=self.holdings[symbol]*per_share
        tax=gross*costs.dividend_tax
        self.distributions+=gross
        self.taxes+=tax
        if gross>tax:
            self.pending.append((due,'tqqq_dividend' if symbol=='TQQQ' else 'core_dividend',gross-tax))

    def release(self,session):
        remaining=[]
        for due,source,amount in self.pending:
            if due<=session:
                self.pockets[source]+=amount
            else:
                remaining.append((due,source,amount))
        self.pending=remaining

    def accrue(self,annual_rate,calendar_days,costs):
        rate=annual_rate*costs.cash_yield_multiplier*D(calendar_days)/365
        for keys,target in ((TACTICAL_POCKETS,'tactical_interest'),
                            ((CORE_CASH,'core_dividend','core_interest'),'core_interest')):
            gross=sum(self.pockets[k] for k in keys)*rate
            tax=gross*costs.short_tax
            self.pockets[target]+=gross-tax
            self.taxes+=tax
            self.interest_income+=gross

    def value(self,prices):
        return sum(self.pockets.values())+sum(x[2] for x in self.pending)+sum(
            self.holdings[s]*prices[s] for s in SYMBOLS)

    def snapshot(self,frame,policy):
        from .planner import Snapshot
        frame.validate(policy)
        if self.holdings['TQQQ'] and (frame.tqqq_prior_close is None
                or not frame.tqqq_prior_close.is_finite() or frame.tqqq_prior_close<=0):
            raise ValueError('tqqq_prior_close_unverified')
        nav=self.value(frame.prices)
        tactical_cash=sum(self.pockets[k] for k in TACTICAL_POCKETS)
        tactical_value=tactical_cash+self.holdings['TQQQ']*frame.prices['TQQQ']+sum(
            amount for _,source,amount in self.pending if source in TACTICAL_POCKETS)
        self.account.mark(nav,self.flow)
        if 0<=self.risk.cooldown_through<frame.session and not self.holdings['TQQQ']:
            self.tactical.restart(tactical_value)
            self.risk.cooldown_through=-1
        else:
            self.tactical.mark(tactical_value,self.tactical_flow)
        self.flow=self.tactical_flow=D(0)
        return Snapshot(frame,nav,sum(self.pockets.values()),self.pockets[CORE_CASH],tactical_cash,
                        dict(self.holdings),self.account.loss,self.tactical.loss)

    def fill(self,order,qty,snap,costs,policy,due,limits=None):
        authorize(order.symbol,order.side)
        if (not 0<qty<=order.quantity or qty!=down(qty,SHARE)
                or snap.quantities!=self.holdings or snap.nav!=self.value(snap.prices)):
            raise ValueError('stale_snapshot_or_fill_size')
        symbol=order.symbol
        price=costs.price(snap.prices[symbol],order.side)
        gross=qty*price
        fee=costs.commission if order.side=='BUY' else min(costs.commission,gross)
        if order.side=='BUY':
            keys=(CORE_CASH,) if symbol in CORE else TACTICAL_POCKETS
            spend=gross+fee
            if spend>min(sum(self.pockets[k] for k in keys),snap.available-D(policy['execution']['cash_buffer'])):
                raise ValueError('insufficient_verified_cash')
            if symbol=='TQQQ':
                if limits is None or not limits.allow_buy or order.cap is None:
                    raise ValueError('tactical_buy_restricted')
                mark=snap.prices[symbol]
                post_nav=snap.nav-(price-mark)*qty-fee
                cap=min(order.cap,limits.cap,limits.target,D('.20'))
                if (post_nav<=0 or (qty+self.holdings[symbol])*mark>cap*post_nav
                        or (qty+self.holdings[symbol])*mark/post_nav-self.holdings[symbol]*mark/snap.nav>limits.max_add_nav):
                    raise ValueError('tqqq_cap_or_add_limit')
                self.lots.append((snap.frame.day,qty,spend))
                self.risk.last_add=snap.session
            for key in keys:
                used=min(self.pockets[key],spend)
                self.pockets[key]-=used
                spend-=used
            self.holdings[symbol]+=qty
        else:
            if qty>self.holdings[symbol]:
                raise ValueError('no_short_or_oversell')
            trade_day=date.fromisoformat(snap.frame.day)
            remaining=qty
            tax=realized=D(0)
            lots=[]
            for day,shares,basis in self.lots:
                sold=min(shares,remaining)
                basis_sold=basis*sold/shares
                gain=sold*(price-fee/qty)-basis_sold
                tax+=max(D(0),gain)*(costs.long_tax if (trade_day-date.fromisoformat(day)).days>365 else costs.short_tax)
                realized+=gain
                remaining-=sold
                if shares>sold:
                    lots.append((day,shares-sold,basis-basis_sold))
            if remaining or gross<fee+tax:
                raise ValueError('sale_lots_or_fees_unreconciled')
            self.lots=lots
            self.holdings[symbol]-=qty
            self.taxes+=tax
            self.realized_gain+=realized
            net=gross-fee-tax
            self.sale_proceeds+=net
            self.pending.append((due,'tqqq_sale',net))
            if not self.holdings[symbol]:
                self.risk.flat(snap.session,policy)
        self.commissions+=fee
        self.volume+=gross
        self.trades+=1
        return price

    def save(self):
        return json.dumps(asdict(self),default=str,sort_keys=True)

    @classmethod
    def restore(cls,body):
        row=json.loads(body)
        for k in ('holdings','pockets'):
            row[k]={symbol:D(value) for symbol,value in row[k].items()}
        row['pending']=[(due,source,D(value)) for due,source,value in row['pending']]
        row['lots']=[(day,D(qty),D(basis)) for day,qty,basis in row['lots']]
        for k in ('account','tactical'):
            row[k]=Drawdown(**{key:D(value) for key,value in row[k].items()})
        for key in ('highest_close','half_limit'):
            value=row['risk'][key]
            row['risk'][key]=D(value) if value is not None else None
        row['risk']=RiskState(**row['risk'])
        for key,value in tuple(row.items()):
            if isinstance(value,str):
                row[key]=D(value)
        return cls(**row)


class PaperBook:
    """Atomically persist one unresolved order, fills, and the account snapshot."""
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=5,isolation_level=None)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS wallet(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, facts TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, body TEXT NOT NULL, done TEXT NOT NULL,
              status TEXT NOT NULL, cash_left TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON orders((1)) WHERE status IN ('OPEN','PARTIAL','UNKNOWN');
          CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, order_id TEXT NOT NULL, facts TEXT NOT NULL);
        """)
        self.db.execute('INSERT OR IGNORE INTO wallet VALUES(1,?)',(Wallet().save(),))

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')

    def account(self):
        return Wallet.restore(self.db.execute('SELECT body FROM wallet WHERE id=1').fetchone()[0])

    def _save(self,account):
        self.db.execute('UPDATE wallet SET body=? WHERE id=1',(account.save(),))

    def event(self,key,facts,apply):
        payload=json.dumps(facts,default=str,sort_keys=True)
        with self.transaction():
            existing=self.db.execute('SELECT facts FROM events WHERE id=?',(key,)).fetchone()
            if existing:
                if existing[0]!=payload:
                    raise ValueError('changed_paper_event')
                return False
            account=self.account()
            apply(account)
            self._save(account)
            self.db.execute('INSERT INTO events VALUES(?,?)',(key,payload))
        return True

    def reserve(self,order,frame,signal,costs,policy,due):
        authorize(order.symbol,order.side)
        if signal.day!=frame.reference_day or not order.key.startswith(frame.day+':'):
            raise ValueError('order_session_mismatch')
        payload=json.dumps(asdict(order),default=str,sort_keys=True)
        with self.transaction():
            existing=self.db.execute('SELECT body FROM orders WHERE id=?',(order.key,)).fetchone()
            if existing:
                if existing[0]!=payload:
                    raise ValueError('changed_duplicate_order')
                return False
            if self.active():
                raise ValueError('unresolved_paper_order')
            probe=self.account()
            snap=probe.snapshot(frame,policy)
            limits=assess(signal,snap,probe.risk,policy)
            price=probe.fill(order,order.quantity,snap,costs,policy,due,limits)
            reserved=order.quantity*price+costs.commission if order.side=='BUY' else D(0)
            self.db.execute("INSERT INTO orders VALUES(?,?,'0','OPEN',?)",(order.key,payload,str(reserved)))
        return True

    def active(self):
        return self.db.execute("SELECT id,status FROM orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')").fetchone()

    def cancel(self,key):
        with self.transaction():
            if self.db.execute("UPDATE orders SET status='CANCELED' WHERE id=? AND status IN ('OPEN','PARTIAL')",(key,)).rowcount!=1:
                raise ValueError('not_cancelable')

    def mark_unknown(self,key):
        with self.transaction():
            if self.db.execute("UPDATE orders SET status='UNKNOWN' WHERE id=? AND status IN ('OPEN','PARTIAL')",(key,)).rowcount!=1:
                raise ValueError('order_not_unresolved')

    def reserved_cash(self):
        row=self.db.execute("SELECT cash_left FROM orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')").fetchone()
        return D(row[0]) if row else D(0)

    def fill(self,key,fill_id,qty,frame,signal,costs,policy,due):
        if signal.day!=frame.reference_day or not key.startswith(frame.day+':'):
            raise ValueError('signal_not_from_previous_completed_session')
        with self.transaction():
            body,done,status,cash_left=self.db.execute('SELECT body,done,status,cash_left FROM orders WHERE id=?',(key,)).fetchone()
            facts=json.dumps(dict(qty=str(qty),day=frame.day,prices=frame.prices,costs=asdict(costs),due=due),
                             default=str,sort_keys=True)
            existing=self.db.execute('SELECT order_id,facts FROM fills WHERE id=?',(fill_id,)).fetchone()
            if existing:
                if existing!=(key,facts):
                    raise ValueError('changed_duplicate_fill')
                return False
            if status not in ('OPEN','PARTIAL'):
                raise ValueError('order_not_fillable')
            row=json.loads(body)
            row['quantity']=D(row['quantity'])
            row['cap']=D(row['cap']) if row['cap'] is not None else None
            order=Order(**row)
            previously_filled=D(done)
            done=previously_filled+qty
            if done>order.quantity:
                raise ValueError('partial_fill_overrun')
            account=self.account()
            snap=account.snapshot(frame,policy)
            last_add=account.risk.last_add
            if previously_filled and order.symbol=='TQQQ' and order.side=='BUY':
                # A same-session partial belongs to the original add, subject
                # to the remaining reservation and every other fresh risk check.
                account.risk.last_add=-1000000
            risk=assess(signal,snap,account.risk,policy)
            account.risk.last_add=last_add
            fill_costs=replace(costs,commission=D(0)) if previously_filled else costs
            remaining_cash=D(cash_left)
            if order.side=='BUY':
                remaining_cash-=qty*fill_costs.price(snap.prices[order.symbol],'BUY')+fill_costs.commission
                if remaining_cash<0:
                    raise ValueError('fill_exceeds_cash_reservation')
            account.fill(order,qty,snap,fill_costs,policy,due,risk)
            self._save(account)
            self.db.execute('INSERT INTO fills VALUES(?,?,?)',(fill_id,key,facts))
            self.db.execute('UPDATE orders SET done=?,status=?,cash_left=? WHERE id=?',
                            (str(done),'FILLED' if done==order.quantity else 'PARTIAL',str(remaining_cash),key))
        return True
