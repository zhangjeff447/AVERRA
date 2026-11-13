"""Frozen paper-only strategy configuration."""
from decimal import Decimal
import json
from pathlib import Path


D = Decimal
CORE = ('VOO', 'SMH')
SYMBOLS = (*CORE, 'TQQQ')


def load_policy(path=None):
    with Path(path or Path(__file__).with_name('policy.json')).open() as handle:
        cfg = json.load(handle)
    if cfg['mode'] != 'PAPER_ONLY' or D(cfg['risk']['absolute_tqqq_cap']) > D('.20'):
        raise ValueError('policy_permission_violation')
    for row in cfg['states'].values():
        if (set(row['new_core_cash']) != set(CORE)
                or sum(D(x) for x in row['new_core_cash'].values()) != 1
                or not 0 <= D(row['tqqq_target']) <= D(row['tqqq_cap']) <= D(cfg['risk']['absolute_tqqq_cap'])):
            raise ValueError('policy_weights_invalid')
    return cfg
