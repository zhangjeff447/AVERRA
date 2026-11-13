"""Observe completed QQQ bars. Execution consumes the preceding session only."""
from dataclasses import dataclass
from math import isfinite, sqrt

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    day: str
    state: str
    close: float
    ma50: float
    ma200: float
    ret20: float
    vol20: float
    below_ma50: int

    def __post_init__(self):
        if (not all(isfinite(x) for x in (self.close,self.ma50,self.ma200,self.ret20,self.vol20))
                or min(self.close,self.ma50,self.ma200)<=0 or self.vol20<0
                or self.ret20<=-1 or type(self.below_ma50) is not int or self.below_ma50<0):
            raise ValueError('unverified_signal')


def classify(close, fast, slow, ret, vol, market):
    rules = {
        'DEFENSIVE': close <= slow or vol > float(market['emergency_volatility']),
        'STRONG_BULL': (close > fast > slow and fast / slow - 1 >= float(market['strong_min_ma_spread'])
                        and ret >= float(market['strong_min_return'])
                        and vol < float(market['strong_buy_volatility_ceiling'])),
        'RISK_ON': (close > slow and fast > slow and ret > float(market['risk_on_min_return'])
                    and vol < float(market['buy_volatility_ceiling'])),
        'RECOVERY': True,
    }
    return next(state for state in market['priority'] if rules[state])


def signals(close, market):
    if (not close.index.is_monotonic_increasing or close.index.has_duplicates
            or not np.isfinite(close).all() or (close <= 0).any()):
        raise ValueError('invalid_qqq_history')
    metrics = pd.DataFrame(dict(close=close, ma50=close.rolling(market['fast_days']).mean(),
        ma200=close.rolling(market['slow_days']).mean(), ret20=close.pct_change(market['return_days']),
        vol20=close.pct_change().rolling(market['volatility_days']).std(ddof=1)
              * sqrt(market['annual_sessions']))).dropna()
    state = 'DEFENSIVE'
    pending = None
    count = below = 0
    output = {}
    for day, px, fast, slow, ret, vol in metrics.itertuples(name=None):
        raw = classify(px, fast, slow, ret, vol, market)
        below = below + 1 if px < fast else 0
        if raw != pending:
            pending, count = raw, 0
        count += 1
        confirm = market['strong_confirm_days'] if raw == 'STRONG_BULL' else market['ordinary_confirm_days']
        if count >= confirm:
            state = raw
        output[day] = Signal(day, state, px, fast, slow, ret, vol, below)
    return output
