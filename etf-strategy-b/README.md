# ETF Strategy B v2.1 · Core VOO / SMH, no TQQQ

**Status (2026-11-14): release candidate v2.1.1.** It has passed the full test suite and a sandboxed install and rollback rehearsal. The production cutover is planned for no earlier than 2026-11-23, after five clean trading sessions on v2.0. Until then, v2.0 remains the live version.

This folder contains the strategy, the offline backtest entry point, and the integration source that differs from the base executor (`integration/`). The base executor, its runtime configuration, credentials and deployment workflow are not published.

## Rules

- **Buy-only, two ETFs.** VOO and SMH are bought and never sold. TQQQ and QQQ have no order permission. QQQ is used only as the market signal.
- **Funding.** Only cash the broker reports as settled, unleveraged and owned is used. Pending deposits wait until they settle.
- **Daily cap.** Combined VOO + SMH buys are capped at **$200 per trading day**. Cash above the cap rolls into the next sessions. The cap is checked when the plan is made and checked again independently right before each order is submitted.
- **Long-term weights.** Measured on the combined market value of the two ETFs: VOO 20%, SMH 80%.
- **Floors.** If VOO falls below 15% of the core, new cash tops it up first. If SMH falls below 70%, new cash tops it up first, **except in the Defensive regime**, where the SMH top-up is suspended so that defensive cash really goes 80/20 to VOO.
- **New cash by regime:**

| Regime | New cash VOO / SMH |
| --- | --- |
| Defensive | 80% / 20% |
| Recovery | 50% / 50% |
| Risk-on | 30% / 70% |
| Strong bull | 10% / 90% |

- **Regime signal.** Based on completed, adjusted QQQ daily bars: MA50, MA200, 20-day return and 20-day annualized volatility. A regime must hold for 3 consecutive days to take effect, or 5 days for Strong bull.
- **Schedule.** Orders can be placed at 10:05, 12:30 and 15:30 ET. Once a day's allocation is fixed, money that arrives later that day is planned on the next session.

## Changes from v2.0

1. **Defensive allocation fix.** In v2.0, the SMH floor could send 100% of new cash to SMH while in the Defensive regime. With $100 weekly deposits, defensive cash actually went only 32–48% to VOO. In v2.1 it is exactly 80/20.
2. **$200 daily buy cap**, enforced both when planning and in the execution layer.
3. **Post-fill valuation lag.** When the broker's total value lags the agent's own fills, the agent retries the read. If the totals still disagree, it keeps the fills and places no further orders that session. Reports read the day's fills from the ledger, so a completed purchase is never reported as "WAIT".
4. **Effective permissions** shown by `--check-config` are now derived from the order-authorization rules actually in force, rather than being hard-coded.

## Backtests

The simulation uses the previous session's signal and fills at the next session's open, with 5 bps slippage per side. Results are hypothetical and do not include live-window execution differences.

| Scenario | v2.0 final value / max drawdown | v2.1 final value / max drawdown |
| --- | ---: | ---: |
| 2013-01-02 → 2026-11-11, $10,000 start + $400/month | $771,312 / 41.10% | $776,956 / 41.22% |
| 2013 → 2026-11, $1,450 start (20/80) + $100/week | $625,559 / 41.03% | $623,317 / 41.01% |
| 2021 → 2026-11, $1,450 start (20/80) + $100/week | $82,712 / 39.41% | $79,085 / 37.40% |
| **Stress test** 2001-03-21 → 2012-12-31, $1,450 + $100/week ¹ | $73,017 / 63.29% | $73,869 / 59.12% |

¹ SPY stands in for VOO, which launched in 2010. Early SMH history is spliced from Semiconductor HOLDRS, which converted 1:1 into the current ETF in 2011. The CAGR over this period was 0.06% for v2.0 and 0.72% for v2.1.

**Read the stress test.** The portfolio holds roughly 80–85% semiconductors. In a semiconductor bear market like 2000–2002, the regime rules soften the drawdown, but the strategy can still lose more than half its value and take many years to recover. The 2013–2026 results come from an exceptional period for semiconductors.

Research notes from the same review: adding more intraday buy windows, splitting deposits across days, dip-waiting rules and machine-learning allocators were all tested. None improved the final value consistently out of sample. Even perfect foresight of each day's low adds only about +0.85% over ten years.

## Run locally

Use Python 3.11 or later. Prepare NYSE-aligned `VOO.csv`, `SMH.csv`, `TQQQ.csv` and `QQQ.csv`, each with `Date, Open, Close, Adj Close, Dividends, Stock Splits`. Also provide `DGS3MO.csv`, with the date in the first column and a `DGS3MO` column. The TQQQ file keeps the shared market-data format compatible. Leave at least 200 trading days of warm-up data before the start date.

```sh
python -m pip install -r requirements.txt

# $10,000 start, $400 on the first session of each month, $200 daily cap
python replay.py --data ./market-data --start 2016-01-04

# $1,450 start, $100 every week
python replay.py --data ./market-data --start 2016-01-04 --seed 1450 --weekly 100

# disable the daily cap for comparison
python replay.py --data ./market-data --daily-limit none
```

## Layout

| Path | Contents |
| --- | --- |
| `strategy_b/` | Policy (`policy.json`), regime signals, planner, paper wallet and backtest engine |
| `replay.py` | Command-line backtest |
| `integration/agent/` | Executor files that differ from the unpublished base executor (sizing, daily cap, execution guard, reports). They do not run on their own. |

This is research software, not investment advice. The live agent uses a broker connection that is not part of this repository.
