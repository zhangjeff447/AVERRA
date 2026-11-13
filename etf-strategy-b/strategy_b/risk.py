"""Position and account protections independent of signal classification."""
from dataclasses import dataclass
from decimal import Decimal

from .policy import D


@dataclass
class Drawdown:
    previous: D = D(0)
    index: D = D(1)
    peak: D = D(1)

    def mark(self, value, flow=D(0)):
        if value < 0 or (self.previous + flow < 0):
            raise ValueError('invalid_equity_mark')
        base = self.previous + flow
        if base > 0:
            self.index *= value / base
        self.previous = value
        self.peak = max(self.peak, self.index)

    @property
    def loss(self):
        return 1 - self.index / self.peak

    def restart(self, value):
        self.previous, self.index, self.peak = value, D(1), D(1)


@dataclass
class RiskState:
    review_required: bool = False
    tactical_exit: bool = False
    liquidation_reason: str = ''
    cooldown_through: int = -1
    flat_session: int = -1
    last_add: int = -1000000
    highest_close: D = D(0)
    last_close_session: int = -1
    half_limit: D | None = None

    def completed_close(self, session, price, qty):
        if session > self.last_close_session:
            self.highest_close = max(self.highest_close, price) if qty else D(0)
            self.last_close_session = session

    def flat(self, session, policy):
        self.highest_close = D(0)
        self.flat_session = session
        self.half_limit = None
        self.liquidation_reason = ''
        if self.tactical_exit:
            self.cooldown_through = session + policy['risk']['tactical_cooldown_sessions']
            self.tactical_exit = False


@dataclass(frozen=True)
class Limits:
    target: Decimal
    cap: Decimal
    allow_buy: bool
    max_add_nav: Decimal
    half_limit: Decimal | None
    flags: tuple[str, ...]
    liquidation: bool


def assess(signal, snap, risk, policy):
    cfg = policy['risk']
    market = policy['market']
    mode = policy['states'][signal.state]
    qty = snap.quantities['TQQQ']
    account_dd, tactical_dd = snap.account_dd, snap.tactical_dd
    cooldown = risk.cooldown_through >= snap.session
    risk.review_required |= account_dd >= D(cfg['account_review_drawdown'])
    if not cooldown and tactical_dd >= D(cfg['tactical_exit_drawdown']):
        risk.tactical_exit = True

    flags = []
    trailing = (qty and risk.highest_close and snap.tqqq_prior_close is not None
                and snap.tqqq_prior_close <= risk.highest_close * (1 - D(cfg['trailing_drawdown'])))
    checks = {
        'ACCOUNT_REVIEW': risk.review_required,
        'TACTICAL_LIQUIDATION': risk.tactical_exit,
        'QQQ_BELOW_MA200': signal.close < signal.ma200,
        'VOLATILITY_EMERGENCY': signal.vol20 > float(market['emergency_volatility']),
        'TQQQ_TRAILING_STOP': trailing,
    }
    for key in cfg['priority']:
        if checks.get(key):
            flags.append(key)
    if qty and flags:
        risk.liquidation_reason = flags[0]
    if risk.liquidation_reason and qty and risk.liquidation_reason not in flags:
        flags.insert(0, risk.liquidation_reason)
    liquidation = bool(flags)
    if not qty and risk.tactical_exit:
        risk.flat(snap.session, policy)
        cooldown = True

    cap = min(D(mode['tqqq_cap']), D(cfg['absolute_tqqq_cap']))
    target = min(D(mode['tqqq_target']), cap)
    if liquidation:
        cap = target = D(0)
    elif qty * snap.prices['TQQQ'] > snap.nav * D(cfg['absolute_tqqq_cap']):
        flags.append('ABSOLUTE_CAP')
    elif qty * snap.prices['TQQQ'] > snap.nav * cap:
        flags.append('STATE_CAP')

    if signal.below_ma50 < cfg['below_ma50_days']:
        risk.half_limit = None
    elif qty and risk.half_limit is None:
        risk.half_limit = qty * (1 - D(cfg['below_ma50_reduction']))
    if signal.below_ma50 >= cfg['below_ma50_days']:
        flags.append('QQQ_MA50_HALF')
    if account_dd >= D(cfg['account_pause_drawdown']):
        flags.append('ACCOUNT_ADD_PAUSED')
    if tactical_dd >= D(cfg['tactical_pause_drawdown']) or cooldown:
        flags.append('TACTICAL_COOLDOWN' if cooldown else 'TACTICAL_ADD_PAUSED')
    if risk.flat_session == snap.session:
        flags.append('SAME_SESSION_REENTRY_BLOCKED')

    qualifying = (signal.close > signal.ma200 and signal.ma50 > signal.ma200 and signal.ret20 > 0
                  and signal.vol20 < float(market['buy_volatility_ceiling']))
    strong = signal.state == 'STRONG_BULL'
    if strong and (signal.vol20 >= float(market['strong_buy_volatility_ceiling'])
                   or snap.session - risk.last_add < cfg['strong_step_spacing_sessions']):
        flags.append('STRONG_ADD_RESTRICTED')
    if not qualifying:
        flags.append('QQQ_BUY_FILTER')
    max_add = D(cfg['strong_step_nav']) if strong else D(cfg['absolute_tqqq_cap'])
    return Limits(target, cap, target > 0 and not flags, max_add, risk.half_limit, tuple(flags), liquidation)
