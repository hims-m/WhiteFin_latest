"""
Backtest Agent (out-of-sample)
--------------------------------
Answers the question a reviewer will ask first: "is this any better than just
buying a Nifty 50 index fund?"

Method (walk-forward, one split):
  1. Hide the most recent BACKTEST_TEST_DAYS (~1 year) of prices.
  2. Re-run the SAME optimizer, with the same shortlist, objective and
     constraints, using ONLY the older "training" data.
  3. Hold those weights through the hidden year (buy-and-hold) and compare
     against the benchmark and a naive equal-weight version of the shortlist.

Because the weights never saw the test year, this is an honest
out-of-sample result — unlike judging a portfolio on the data it was tuned on.

Known limitation (stated in the output): the shortlist and fundamental scores
use TODAY's fundamentals, which slightly favours the strategy (look-ahead bias).
A full version would rebuild fundamentals as of each historical date.
"""
import numpy as np
import pandas as pd

from config import BACKTEST_TEST_DAYS, RISK_FREE_RATE
from agents.portfolio_optimization import optimize_sleeve
from agents.risk_assessment import max_drawdown

TRADING_DAYS = 252


def _metrics(values: pd.Series) -> dict:
    r = values.pct_change().dropna()
    years = len(r) / TRADING_DAYS
    total = float(values.iloc[-1] / values.iloc[0] - 1)
    ann_ret = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
    vol = float(r.std() * np.sqrt(TRADING_DAYS))
    return {
        "total_return": round(total, 4),
        "annualized_return": round(ann_ret, 4),
        "annual_volatility": round(vol, 4),
        "sharpe_ratio": round((ann_ret - RISK_FREE_RATE) / vol, 3) if vol > 0 else None,
        "max_drawdown": round(max_drawdown(values), 4),
    }


def _buy_and_hold(prices: pd.DataFrame, weights: dict) -> pd.Series:
    cols = [t for t in weights if t in prices.columns]
    w = pd.Series({t: weights[t] for t in cols})
    w = w / w.sum()
    rel = prices[cols] / prices[cols].iloc[0]
    return 100 * (rel * w).sum(axis=1)


def run(portfolio: dict, market_data: dict, fundamentals: dict, strategy_info: dict | None = None) -> dict:
    shortlist = portfolio["equity_shortlist"]
    stocks = market_data["stocks"]
    prices = pd.DataFrame({t: stocks[t]["history"] for t in shortlist}).dropna()
    bench_raw = market_data["benchmark"]["history"]

    caveats = ["Fundamental scores use today's data (mild look-ahead bias)."]
    if (strategy_info or {}).get("decided_by") == "out_of_sample_bakeoff":
        caveats.append("The Strategy Selector used this same test year to pick MPT or HRP, "
                       "so this result is slightly flattering (selection bias).")
    if market_data["data_source"] != "live":
        caveats.insert(0, "Some or all prices are synthetic demo data — these results are illustrative only.")

    if len(prices) < BACKTEST_TEST_DAYS + 250:
        return {"available": False, "reason": f"Need at least {BACKTEST_TEST_DAYS + 250} days of shared history; "
                                              f"only {len(prices)} available.", "caveats": caveats}

    train = prices.iloc[:-BACKTEST_TEST_DAYS]
    test = prices.iloc[-BACKTEST_TEST_DAYS - 1:]
    bench_train = bench_raw[bench_raw.index <= train.index[-1]]

    cons = portfolio["constraints_used"]
    trained = optimize_sleeve(
        train, bench_train,
        sectors={t: fundamentals[t]["sector"] for t in shortlist},
        scores={t: fundamentals[t]["fundamental_score"] for t in shortlist},
        method=cons["method"], max_weight=cons["max_weight"], sector_cap=cons["sector_cap"],
    )

    strategy = _buy_and_hold(test, trained["weights"])
    equal = _buy_and_hold(test, {t: 1.0 for t in shortlist})
    bench = bench_raw.reindex(test.index).ffill().dropna()
    bench = 100 * bench / bench.iloc[0]
    idx = strategy.index.intersection(bench.index)
    strategy, equal, bench = strategy.loc[idx], equal.loc[idx], bench.loc[idx]

    s_m, b_m, e_m = _metrics(strategy), _metrics(bench), _metrics(equal)
    weekly = strategy.index[::5]
    return {
        "available": True,
        "train_period": [train.index[0].strftime("%Y-%m-%d"), train.index[-1].strftime("%Y-%m-%d")],
        "test_period": [test.index[0].strftime("%Y-%m-%d"), test.index[-1].strftime("%Y-%m-%d")],
        "weights_trained_on_past_only": trained["weights"],
        "strategy": s_m,
        "benchmark": {**b_m, "name": market_data["benchmark"]["name"]},
        "equal_weight_shortlist": e_m,
        "excess_return_vs_benchmark": round(s_m["total_return"] - b_m["total_return"], 4),
        "beat_benchmark": s_m["total_return"] > b_m["total_return"],
        "better_risk_adjusted": (s_m["sharpe_ratio"] or -9) > (b_m["sharpe_ratio"] or -9),
        "series": {
            "dates": [d.strftime("%Y-%m-%d") for d in weekly],
            "strategy": [round(float(strategy[d]), 2) for d in weekly],
            "benchmark": [round(float(bench[d]), 2) for d in weekly],
            "equal_weight": [round(float(equal[d]), 2) for d in weekly],
        },
        "caveats": caveats,
    }
