"""Frozen historical bars, corporate actions and lagged cash-yield proxy."""
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import numpy as np
import pandas as pd

from .policy import SYMBOLS


ET = ZoneInfo('America/New_York')
TICKERS = (*SYMBOLS,'QQQ')
FILES = (*(f'{s}.csv' for s in TICKERS),'DGS3MO.csv')


@lru_cache
def exchange():
    return xc.get_calendar('XNYS',start='2011-01-01',end='2036-12-31')


@lru_cache(maxsize=10000)
def session_times(day):
    cal=exchange()
    return (cal.session_open(day).to_pydatetime().astimezone(ET),
            cal.session_close(day).to_pydatetime().astimezone(ET))


@lru_cache(maxsize=10000)
def preceding(day):
    return exchange().previous_session(day).strftime('%Y-%m-%d')


def load(root):
    root=Path(root)
    frames={}
    dates=None
    for symbol in TICKERS:
        data=pd.read_csv(root/(symbol+'.csv'),index_col='Date')
        required=['Open','Close','Adj Close','Dividends','Stock Splits']
        values=data[required]
        if not np.isfinite(values.to_numpy()).all() or (values[['Open','Close','Adj Close']]<=0).any().any():
            raise ValueError('invalid_history:'+symbol)
        expected=exchange().sessions_in_range(data.index[0],data.index[-1]).strftime('%Y-%m-%d')
        if list(data.index)!=list(expected) or (dates is not None and not dates.equals(data.index)):
            raise ValueError('incomplete_exchange_sessions:'+symbol)
        dates=data.index
        split=data['Stock Splits'].replace(0,1)
        if (split<=0).any() or (data['Dividends']<0).any():
            raise ValueError('invalid_company_action:'+symbol)
        # Yahoo OHLC/dividends are already split-normalized. Undo the known
        # share-unit conversion, then apply each split on its effective day.
        factor=split.shift(-1,fill_value=1).iloc[::-1].cumprod().iloc[::-1]
        frames[symbol]=pd.DataFrame(dict(open=data['Open']*factor,close=data['Close']*factor,
             adjusted=data['Adj Close'],dividend=data['Dividends']*factor,split=split),index=dates)
    rates=pd.read_csv(root/'DGS3MO.csv',index_col=0)['DGS3MO']
    rates=pd.to_numeric(rates,errors='coerce').dropna()/100
    rates=rates.reindex(rates.index.union(dates)).sort_index().ffill().reindex(dates).shift(2)
    if (rates.dropna()<0).any():
        raise ValueError('cash_rate_invalid')
    return frames,rates,{name:sha256((root/name).read_bytes()).hexdigest() for name in FILES}
