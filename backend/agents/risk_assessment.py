"""
Risk Assessment Agent
------------------------
Per-stock risk metrics (runs in PARALLEL with the Fundamental Analysis Agent —
both only need market data) plus portfolio-level risk helpers that the
Portfolio Optimization and Validator agents now actually use.

Metrics:
  * annualised volatility        — typical size of yearly swings
  * 1-day 95% VaR                — "on 1 day in 20, expect a loss at least this big"
  * 1-day 95% CVaR (Expected Shortfall) — the AVERAGE loss on those bad days;
                                   VaR says where the tail starts, CVaR how deep it goes
  * maximum drawdown             — worst peak-to-trough fall in the history
  * beta vs Nifty 50             — sensitivity to the market (v1 used an
                                   equal-weight universe; real Nifty is used when available)
"""
import numpy as np
import pandas as pd

TRADING_DAYS = 252


def daily_returns(prices: pd.Series | pd.DataFrame):
    return prices.pct_change().dropna(how="all")


def max_drawdown(prices: pd.Series) -> float:
    running_peak = prices.cummax()
    return float((prices / running_peak - 1).min())


def var_cvar(returns, level: float = 0.95) -> tuple[float, float]:
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) == 0:
        return 0.0, 0.0
    var = float(np.percentile(r, (1 - level) * 100))
    tail = r[r <= var]
    cvar = float(tail.mean()) if len(tail) else var
    return var, cvar


def beta(asset_returns: pd.Series, market_returns: pd.Series) -> float:
    aligned = pd.concat([asset_returns, market_returns], axis=1, join="inner").dropna()
    if len(aligned) < 30 or aligned.iloc[:, 1].var() == 0:
        return 1.0
    cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
    return float(cov[0, 1] / cov[1, 1])


def run(market_data: dict) -> dict:
    stocks = market_data["stocks"]
    market_r = daily_returns(market_data["benchmark"]["history"])

    results = {}
    for ticker, s in stocks.items():
        hist = s["history"]
        r = daily_returns(hist)
        if len(r) < 30:
            results[ticker] = {"annual_volatility": None, "daily_var_95": None, "daily_cvar_95": None,
                               "max_drawdown": None, "beta": None, "data_points": int(len(r)),
                               "source": s["source"], "insufficient_data": True}
            continue
        var, cvar = var_cvar(r)
        results[ticker] = {
            "annual_volatility": round(float(r.std() * np.sqrt(TRADING_DAYS)), 4),
            "daily_var_95": round(var, 4),
            "daily_cvar_95": round(cvar, 4),
            "max_drawdown": round(max_drawdown(hist), 4),
            "beta": round(beta(r, market_r), 2),
            "data_points": int(len(r)),
            "source": s["source"],
            "insufficient_data": False,
        }
    return results


def portfolio_risk(weights: dict, price_df: pd.DataFrame, benchmark: pd.Series | None = None) -> dict:
    """Historical risk of a weighted stock basket (weights need not sum to 1)."""
    cols = [t for t in weights if t in price_df.columns and weights[t] > 0]
    if not cols:
        return {}
    w = np.array([weights[t] for t in cols], dtype=float)
    w = w / w.sum()
    rets = daily_returns(price_df[cols]).dropna()
    port = pd.Series(rets.values @ w, index=rets.index)
    var, cvar = var_cvar(port)
    value = (1 + port).cumprod()
    out = {
        "annual_volatility": round(float(port.std() * np.sqrt(TRADING_DAYS)), 4),
        "daily_var_95": round(var, 4),
        "daily_cvar_95": round(cvar, 4),
        "max_drawdown": round(max_drawdown(value), 4),
    }
    if benchmark is not None:
        out["beta"] = round(beta(port, daily_returns(benchmark)), 2)
    return out
