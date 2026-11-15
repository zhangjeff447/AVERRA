"""Strategy B core-only routing through the existing order and cash guards."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from strategy_b.planner import Frame, Snapshot, core_envelope
from strategy_b.policy import CORE, D, SYMBOLS, load_policy
from strategy_b.signals import Signal, signals

from .buy_limits import core_daily_remaining
from .calendar import ET, previous_session
from .cycle import POLICY, authorize_order, down, money


FUNDING_MODE = 'SETTLED_OWN_CASH'
STATE_LABELS = dict(DEFENSIVE='防御', RECOVERY='恢复', RISK_ON='进攻', STRONG_BULL='强牛')


def rules():
    path = Path(__file__).resolve().parents[1] / 'strategy_b/policy.json'
    if sha256(path.read_bytes()).hexdigest() != POLICY['strategy_b_policy_sha256']:
        raise ValueError('strategy_b_policy_hash_changed')
    return load_policy(path)


def market_signal(closes, reference):
    record = signals(closes, rules()['market']).get(reference)
    if record is None:
        raise ValueError('strategy_b_signal_unavailable')
    return asdict(record)


def context(signal):
    s = Signal(**signal['strategy_b'])
    cfg = rules()
    if signal['date'] != s.day or s.state not in STATE_LABELS:
        raise ValueError('strategy_b_signal_identity')
    risk = []
    if s.close < s.ma200:
        risk.append('QQQ 跌破 200 日线')
    if s.vol20 > float(cfg['market']['emergency_volatility']):
        risk.append('QQQ 20 日年化波动超过 45%')
    return dict(version=POLICY['version'], phase=s.state, strategy_b_state=s.state, signal_date=s.day,
                close=str(s.close), sma50=str(s.ma50), sma200=str(s.ma200), slow_ma_period=200,
                funding_mode=FUNDING_MODE, strategic_core_weights=cfg['strategic_core_weights'],
                cash_weights=cfg['states'][s.state]['new_core_cash'], tactical_target='0',
                risk_signals=risk, qqq_ret20=s.ret20, qqq_vol20=s.vol20)


def plan(snapshot, signal, journal, *, now, exclude_intent=None, risk_only=False, persist_allocation=False):
    result = context(signal)
    cfg = rules()
    day = now.astimezone(ET).date().isoformat()
    if result['signal_date'] != previous_session(day):
        raise ValueError('signal_session_stale')
    safe = down(money(snapshot['safe_cash']))
    minimum = money(POLICY['broker_minimum'])
    daily = core_daily_remaining(journal,now,exclude_intent=exclude_intent)
    key = 'strategy_b_v21_core_allocation:' + day
    saved = journal.load(key)
    if saved is None:
        frame = Frame(0, day, signal['date'], now, now,
                      {s: money(snapshot['quotes'][s]['last']) for s in SYMBOLS})
        quantities = {s: money(snapshot['quantities'][s]) for s in SYMBOLS}
        snap = Snapshot(frame, money(snapshot['net_assets']), safe+D(cfg['execution']['cash_buffer']),
                        safe, D(0), quantities, D(0), D(0))
        budgets = core_envelope(snap, result['cash_weights'], cfg['core_floor'], cfg,
                                state=result['phase'], daily_remaining=daily)
        saved = dict(policy_version=POLICY['version'], signal_date=signal['date'], state=result['phase'],
                     budget=str(sum(budgets.values())), amounts={s:str(amount) for s,amount in budgets.items()})
        if not risk_only and persist_allocation and any(value >= minimum for value in budgets.values()):
            with journal.transaction():
                journal.save(key, saved)
    if (saved['policy_version'] != POLICY['version'] or saved['signal_date'] != signal['date']
            or saved['state'] != result['phase'] or set(saved['amounts']) != set(CORE)):
        raise ValueError('strategy_b_allocation_mismatch')
    budgets = {s: money(value) for s,value in saved['amounts'].items()}
    if sum(budgets.values()) != money(saved['budget']) or any(value != down(value) for value in budgets.values()):
        raise ValueError('strategy_b_allocation_invalid')
    result.update(selected=None, safe_cash=str(safe), net_assets=str(snapshot['net_assets']),
                  core_allocation=saved['amounts'], daily_buy_remaining=str(daily),
                  reason='WAIT_STOP_NOT_TRIGGERED' if risk_only else 'WAIT_DAILY_BUY_LIMIT' if daily < minimum
                  else 'WAIT_STRATEGY_B_CASH' if safe < minimum else 'WAIT_STRATEGY_B_NO_CORE_ORDER')
    if risk_only:
        return result
    for symbol in CORE:
        reason = 'STRATEGY_B_CORE_CASH'
        decision_id = ':'.join((day, symbol, 'BUY', reason))
        amount = down(min(budgets[symbol], safe, daily))
        if amount < minimum or journal.action_used(decision_id, exclude_intent=exclude_intent):
            continue
        authorize_order(symbol, 'BUY')
        result.update(reason=reason, selected=dict(symbol=symbol, side='BUY', reason=reason,
            price=str(money(snapshot['quotes'][symbol]['ask'])), amount=str(amount), rotation_amount='0',
            decision_id=decision_id, policy_version=POLICY['version']))
        break
    return result
