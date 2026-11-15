"""Next-open simulation with the same sizing and fill guards as the paper ledger."""
from dataclasses import dataclass
from datetime import date
from math import sqrt

import pandas as pd

from .data import session_times
from .paper import Wallet
from .planner import Frame, core_envelope, core_order, tactical_order
from .policy import CORE, D, SYMBOLS
from .risk import assess
from .signals import signals


@dataclass(frozen=True)
class Bar:
    opened: dict
    closed: dict
    splits: dict
    dividends: dict
    rate: D


def bars(frames,rates):
    output={}
    for day in frames['QQQ'].index:
        fields={field:{s:D(str(frames[s].at[day,field])) for s in SYMBOLS}
                for field in ('open','close','split','dividend')}
        rate=rates.at[day]
        output[day]=Bar(fields['open'],fields['close'],fields['split'],fields['dividend'],
                        D(str(rate)) if pd.notna(rate) else D(0))
    return output


def run(history, qq, policy, costs, *, start, end, variant, seed=D('10000'), monthly=D('400'),
        tactical_fraction=D('.10'), book=None, weekly=None, initial_core=None, daily_limit=None):
    if variant not in ('VOO','CORE_80_20','B_CORE','B_FULL'):
        raise ValueError('unknown_variant')
    wallet=book.account() if book else Wallet()
    fixed=variant in ('VOO','CORE_80_20')
    signal_map=signals(qq,policy['market'])
    dates=list(history)
    rows=[]
    prior_period=None
    for i,day in enumerate(dates):
        if not start<=day<=end:
            continue
        prior=dates[i-1]
        signal=signal_map.get(prior)
        if signal is None:
            raise ValueError('warmup_or_prior_signal_missing')
        bar=history[day]
        if pd.isna(qq[prior]):
            raise ValueError('signal_history_gap')
        opened,closed=session_times(day)
        ref_close=history[prior].closed['TQQQ']/bar.splits['TQQQ']
        opening=Frame(i,day,prior,opened,opened,bar.opened,ref_close)
        closing=Frame(i,day,prior,closed,closed,bar.closed,ref_close)
        period = date.fromisoformat(day).isocalendar()[:2] if weekly is not None else day[:7]
        contribution = monthly if weekly is None else weekly
        deposit = seed if not rows else contribution if period != prior_period else D(0)
        tactical_seed=deposit*tactical_fraction if variant=='B_FULL' and not rows else D(0)
        prior_period=period
        due=i+(1 if day>='2024-05-28' else 2 if day>='2017-09-05' else 3)

        def open_account(account):
            if rows:
                account.accrue(bar.rate,(date.fromisoformat(day)-date.fromisoformat(prior)).days,costs)
            account.release(i)
            if fixed:
                # Buy-and-hold benchmark distributions are reinvested when
                # released; Strategy B's core consumes external funds only.
                account.pockets['external_core']+=account.pockets['core_dividend']
                account.pockets['core_dividend']=D(0)
            for symbol in SYMBOLS:
                if bar.splits[symbol]!=1:
                    account.split(symbol,bar.splits[symbol])
                if bar.dividends[symbol]:
                    account.dividend(symbol,bar.dividends[symbol],i+10,costs)
            if deposit:
                account.deposit(deposit,tactical_seed)
            if not rows and initial_core is not None:
                for symbol, weight in initial_core.items():
                    amount = deposit * weight
                    account.holdings[symbol] = amount / bar.opened[symbol]
                    account.pockets['external_core'] -= amount
            snap=account.snapshot(opening,policy)
            if variant=='B_FULL':
                assess(signal,snap,account.risk,policy)

        if book:
            applied=book.event(day+':OPEN',dict(deposit=deposit,quote=bar.opened),open_account)
            if not applied:
                raise ValueError('paper_day_already_observed_use_saved_book')
            wallet=book.account()
        else:
            open_account(wallet)

        orders=[]
        flags=()

        def trade(order,limits=None):
            nonlocal wallet
            snap=wallet.snapshot(opening,policy)  # Revalue immediately before each fill.
            if book:
                if book.reserve(order,opening,signal,costs,policy,due):
                    book.fill(order.key,order.key+':fill:1',order.quantity,opening,signal,costs,policy,due)
                wallet=book.account()
            else:
                wallet.fill(order,order.quantity,snap,costs,policy,due,limits)
            orders.append(dict(symbol=order.symbol,side=order.side,reason=order.reason,shares=float(order.quantity),
                               gross=float(order.quantity*costs.price(opening.prices[order.symbol],order.side))))

        snap=wallet.snapshot(opening,policy)
        limits=assess(signal,snap,wallet.risk,policy) if variant=='B_FULL' else None
        tactic=tactical_order(signal,snap,limits,costs,policy) if limits else None
        if limits:
            flags=limits.flags
        weights=({'VOO':'1','SMH':'0'} if variant=='VOO' else policy['strategic_core_weights']
                 if fixed else policy['states'][signal.state]['new_core_cash'])
        floor=dict.fromkeys(CORE,'0') if fixed else policy['core_floor']
        envelopes=core_envelope(snap,weights,floor,policy,state=signal.state,daily_remaining=daily_limit)
        cap_bound = daily_limit is not None and snap.core_cash > daily_limit + D(policy['execution']['cash_buffer'])
        if tactic and tactic.side=='SELL':
            # Complete higher-priority risk reduction before accepting core
            # orders on a later observation.
            trade(tactic,limits)
        else:
            for symbol in CORE:
                fresh=wallet.snapshot(opening,policy)
                order=core_order(symbol,envelopes[symbol],fresh,costs,policy)
                if order:
                    trade(order)
            if limits:
                fresh=wallet.snapshot(opening,policy)
                limits=assess(signal,fresh,wallet.risk,policy)
                order=tactical_order(signal,fresh,limits,costs,policy)
                if order:
                    trade(order,limits)

        def close_account(account):
            account.risk.completed_close(i,bar.closed['TQQQ'],account.holdings['TQQQ'])
            account.snapshot(closing,policy)
        if book:
            book.event(day+':CLOSE',dict(quote=bar.closed),close_account)
            wallet=book.account()
        else:
            close_account(wallet)
        nav=wallet.value(bar.closed)
        weight=wallet.holdings['TQQQ']*bar.closed['TQQQ']/nav
        rows.append(dict(date=day,nav=float(nav),unit=float(wallet.account.index),deposit=float(deposit),
            cash=float(sum(wallet.pockets.values())),tqqq_weight=float(weight),state=signal.state,
            signal_date=prior,benchmark_rate=float(bar.rate),risk_flags=list(flags),orders=orders,
            core_envelope={s:float(x) for s,x in envelopes.items()}, cap_bound=cap_bound,
            tactical_target=float(limits.target) if limits else 0,
            tactical_cap=float(limits.cap) if limits else 0))
    return rows,wallet


def metrics(rows,wallet):
    data=pd.DataFrame(rows).set_index('date')
    unit=data['unit']
    years=(date.fromisoformat(unit.index[-1])-date.fromisoformat(unit.index[0])).days/365.2425
    returns=unit.pct_change()
    returns.iloc[0]=unit.iloc[0]-1
    excess=returns-data['benchmark_rate']/252
    downside=sqrt(float(excess.clip(upper=0).pow(2).mean()))
    maxdd=float((1-unit/unit.cummax().clip(lower=1)).max())
    annual=unit.iloc[-1]**(1/years)-1
    calendar_returns=(1+returns).groupby(data.index.str[:4]).prod()-1
    years_complete=calendar_returns.iloc[:-1] if not data.index[-1][5:].startswith('12') else calendar_returns
    worst=years_complete.idxmin() if not years_complete.empty else calendar_returns.idxmin()
    std=float(excess.std(ddof=1))
    rolling={}
    for n in (3,5,10):
        points=(unit/unit.shift(n*252)).dropna().pow(1/n)-1
        rolling[str(n)]=dict(windows=len(points),min=float(points.min()),median=float(points.median()),
            max=float(points.max()),last=float(points.iloc[-1])) if len(points) else dict(windows=0)
    return dict(cagr=float(annual),max_drawdown=maxdd,sharpe=float(excess.mean()*sqrt(252)/std) if std else None,
        sortino=float(excess.mean()*sqrt(252)/downside) if downside else None,calmar=float(annual/maxdd) if maxdd else None,
        annual_turnover=float(wallet.volume)/float(data['nav'].mean())/years,trades=wallet.trades,
        worst_year=str(worst),worst_year_return=float(calendar_returns[worst]),rolling=rolling,
        ending_nav=float(data['nav'].iloc[-1]),ending_cash=float(data['cash'].iloc[-1]),deposits=float(wallet.deposits),
        tqqq_buys=sum(o['side']=='BUY' and o['symbol']=='TQQQ' for row in rows for o in row['orders']),
        tqqq_sells=sum(o['side']=='SELL' and o['symbol']=='TQQQ' for row in rows for o in row['orders']),
        max_tqqq_close_weight=float(data['tqqq_weight'].max()),
        passive_close_over_20=int((data['tqqq_weight']>.20).sum()),
        review_days=sum('ACCOUNT_REVIEW' in row['risk_flags'] for row in rows),tax_paid=float(wallet.taxes),
        realized_tqqq_gain=float(wallet.realized_gain),tqqq_sale_cash_generated=float(wallet.sale_proceeds),
        dividends=float(wallet.distributions),cash_interest=float(wallet.interest_income),fees=float(wallet.commissions))
