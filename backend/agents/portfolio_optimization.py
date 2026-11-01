"""
Portfolio Optimization Agent
--------------------------------
Builds the portfolio in three layers, and RE-RUNS with adjustments whenever
the Validator Agent sends feedback (that loop is what makes this agentic
rather than a one-shot pipeline).

1. POLICY ALLOCATION (Stocks / Bonds / Gold / Cash)
   Start from the risk bucket, then apply two ceilings on equity:
     * glide path by horizon — money needed in 2 years can't ride out a crash,
       whatever the stated risk appetite
     * age rule — max equity ≈ (110 − age)%
   Excess equity moves to bonds (and partly cash for very short horizons).

2. EQUITY SLEEVE (Modern Portfolio Theory, PyPortfolioOpt)
   * Expected returns: CAPM-style equilibrium returns
         mu_i = rf + beta_i × (E[R_market] − rf)  +  fundamental tilt
     instead of "average return over the last 2 years". Historical means are
     extremely noisy; a 2-year bull run produced 30%+ "expected" returns that
     the Monte Carlo then compounded for decades. Betas are estimated from data,
     the market return is a long-run assumption. The fundamental score adds a
     small ±2% "view" (the idea behind Black-Litterman), so fundamentals affect
     weights, not just the shortlist.
   * Risk: Ledoit-Wolf shrunk covariance (kept from v1 — a good choice).
   * Constraints: max weight per stock AND per sector; feasibility is checked
     before solving (6 stocks × 35% is fine, 2 stocks × 35% can never reach 100%).
   * Objective by profile: conservative → min volatility; aggressive → max
     Sharpe (with India's ~6.5% risk-free rate, not PyPortfolioOpt's 2% default);
     moderate → the midpoint of the efficient frontier between those two,
     instead of a hard-coded 18% target. Each attempt uses a FRESH
     EfficientFrontier object (re-using a failed one is unreliable).

3. EXECUTION
   * Portfolio volatility = sqrt(wᵀ Σ w) across asset classes, using the
     measured equity–gold correlation. v1 added volatilities linearly, which
     silently assumes correlation = 1 and erases the point of holding gold.
   * Whole-share quantities via DiscreteAllocation, so ₹50,000 doesn't
     "buy" 0.2 shares of Maruti.
"""
import math
import warnings

import numpy as np
import pandas as pd
from pypfopt import EfficientFrontier, risk_models
from pypfopt.discrete_allocation import DiscreteAllocation
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform

from config import (
    RISK_BUCKETS, HORIZON_EQUITY_CAPS, AGE_RULE_BASE,
    MAX_SINGLE_STOCK_WEIGHT, SECTOR_CAP, MAX_PER_SECTOR_IN_SHORTLIST, NUM_STOCKS_TO_SELECT,
    RISK_FREE_RATE, MARKET_EXPECTED_RETURN, FUNDAMENTAL_TILT,
    BOND_EXPECTED_RETURN, BOND_VOLATILITY, CASH_EXPECTED_RETURN, CASH_VOLATILITY,
    GOLD_EXPECTED_RETURN, GOLD_VOLATILITY_DEFAULT, CORR_EQUITY_BOND, CORR_GOLD_BOND,
    MIN_DIRECT_EQUITY_RUPEES,
)
from agents.risk_assessment import daily_returns, beta as estimate_beta, portfolio_risk

PROFILE_METHOD = {"conservative": "min_volatility", "moderate": "balanced", "aggressive": "max_sharpe"}
STRATEGIES = ("mpt", "hrp")   # chosen per request by the Strategy Selector Agent
TRADING_DAYS = 252


# ======================================================== 1. policy allocation
def policy_allocation(profile: dict, feedback: dict) -> tuple[dict, list[str], float]:
    bucket = dict(RISK_BUCKETS[profile["risk_tolerance"]])
    horizon, age = profile["horizon_years"], profile["age"] or 30
    adjustments = []

    horizon_cap = next((cap for limit, cap in HORIZON_EQUITY_CAPS if horizon < limit), 1.0)
    age_cap = min(0.9, max(0.2, (AGE_RULE_BASE - age) / 100))
    ceiling = min(horizon_cap, age_cap)
    reason = (f"your {horizon}-year horizon" if horizon_cap <= age_cap else f"your age ({age})")

    if feedback.get("equity_reduction"):
        reduced = bucket["stocks"] - feedback["equity_reduction"]
        if reduced < ceiling:
            ceiling = max(0.1, reduced)
            reason = "the portfolio exceeding the volatility limit for your risk profile"

    if bucket["stocks"] > ceiling + 1e-9:
        excess = bucket["stocks"] - ceiling
        adjustments.append(
            f"Equity reduced from {bucket['stocks']:.0%} to {ceiling:.0%} because of {reason}."
        )
        bucket["stocks"] = ceiling
        if horizon < 3:
            bucket["cash"] += excess / 2
            bucket["bonds"] += excess / 2
        else:
            bucket["bonds"] += excess

    bucket = {k: round(v, 4) for k, v in bucket.items()}
    return bucket, adjustments, ceiling


# ======================================================== 2a. shortlist
def select_shortlist(fundamentals: dict, market_data: dict, exclude: list[str],
                     n: int = NUM_STOCKS_TO_SELECT, max_share_price: float | None = None) -> tuple[list[str], list[str]]:
    stocks = market_data["stocks"]
    notes = []
    candidates = [t for t in fundamentals if t not in exclude]
    if max_share_price:
        affordable = [t for t in candidates if stocks[t]["price"] <= max_share_price]
        if len(affordable) >= 3:
            notes.append(f"Only stocks under ₹{max_share_price:,.0f}/share considered, so whole shares fit the budget.")
            candidates = affordable
    live = [t for t in candidates if stocks[t]["source"] == "live"]
    if len(live) >= 3 and len(live) < len(candidates):
        notes.append(f"Excluded {len(candidates) - len(live)} stocks with synthetic data from optimization.")
        candidates = live

    ranked = sorted(candidates, key=lambda t: fundamentals[t]["fundamental_score"], reverse=True)
    picked, per_sector = [], {}
    for t in ranked:
        sec = fundamentals[t]["sector"]
        if per_sector.get(sec, 0) >= MAX_PER_SECTOR_IN_SHORTLIST:
            continue
        picked.append(t)
        per_sector[sec] = per_sector.get(sec, 0) + 1
        if len(picked) == n:
            break
    if len(picked) < min(n, 3):  # tiny universes: relax the sector rule
        picked = ranked[:n]
    return picked, notes


# ======================================================== 2b. equity sleeve
def equilibrium_returns(price_df: pd.DataFrame, benchmark: pd.Series, scores: dict) -> tuple[pd.Series, dict]:
    rets = daily_returns(price_df)
    mkt = daily_returns(benchmark)
    betas = {t: estimate_beta(rets[t], mkt) for t in price_df.columns}
    mu = pd.Series({
        t: RISK_FREE_RATE + betas[t] * (MARKET_EXPECTED_RETURN - RISK_FREE_RATE)
        + FUNDAMENTAL_TILT * (scores.get(t, 50.0) - 50.0) / 50.0
        for t in price_df.columns
    })
    return mu, betas


def _perf(w: pd.Series, mu: pd.Series, S: pd.DataFrame) -> dict:
    ret = float(w @ mu)
    vol = float(np.sqrt(w @ S @ w))
    return {"expected_annual_return": round(ret, 4), "annual_volatility": round(vol, 4),
            "sharpe_ratio": round((ret - RISK_FREE_RATE) / vol, 3) if vol > 0 else None}


# ======================================================== 2b'. Hierarchical Risk Parity
def hrp_weights(price_df: pd.DataFrame) -> pd.Series:
    """Hierarchical Risk Parity (López de Prado, 2016), written out by hand.

    PyPortfolioOpt's HRPOpt crashes with SciPy >= 1.15, and the algorithm is short:
      1. Cluster: distance d_ij = sqrt((1 - corr_ij) / 2), single-linkage tree.
         Stocks that move together end up next to each other.
      2. Quasi-diagonalise: order stocks by the tree's leaves.
      3. Recursive bisection: split the ordered list in half, give each half
         weight in inverse proportion to its variance, repeat inside each half.
    No expected returns are used at all, so HRP cannot be fooled by noisy
    return estimates; it only needs the covariance, and never inverts it.
    """
    r = daily_returns(price_df).dropna()
    tickers = list(price_df.columns)
    if len(tickers) == 1:
        return pd.Series(1.0, index=tickers)
    cov = r.cov().to_numpy()
    corr = r.corr().to_numpy().copy()
    np.fill_diagonal(corr, 1.0)
    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
    np.fill_diagonal(dist, 0.0)
    order = list(leaves_list(linkage(squareform(dist, checks=False), method="single")))

    def cluster_var(idx: list[int]) -> float:
        sub = cov[np.ix_(idx, idx)]
        ivp = 1.0 / np.diag(sub)
        ivp /= ivp.sum()
        return float(ivp @ sub @ ivp)

    w = np.ones(len(tickers))
    clusters = [order]
    while clusters:
        nxt = []
        for c in clusters:
            if len(c) < 2:
                continue
            half = len(c) // 2
            left, right = c[:half], c[half:]
            v_l, v_r = cluster_var(left), cluster_var(right)
            alpha = 1.0 - v_l / (v_l + v_r)
            w[left] *= alpha
            w[right] *= 1.0 - alpha
            nxt += [left, right]
        clusters = nxt
    return pd.Series(w / w.sum(), index=tickers)


def apply_caps(w: pd.Series, sector_map: dict, max_weight: float, sector_cap: float,
               iterations: int = 200) -> pd.Series:
    """Enforce the same per-stock and per-sector caps MPT uses, for weights that
    came from a method without constraints (HRP). Excess weight is moved, in
    proportion, to the positions that still have room. Repeats until stable."""
    w = w.astype(float).copy()
    w = w / w.sum()
    for _ in range(iterations):
        changed = False
        # per-stock cap
        over = w > max_weight + 1e-9
        if over.any():
            excess = float((w[over] - max_weight).sum())
            w[over] = max_weight
            room = w.index[~over & (w < max_weight - 1e-9)]
            if len(room):
                w[room] += excess * w[room] / w[room].sum()
            changed = True
        # per-sector cap
        sec = pd.Series(sector_map).reindex(w.index)
        totals = w.groupby(sec).sum()
        for s_name, tot in totals.items():
            if tot > sector_cap + 1e-9:
                members = sec.index[sec == s_name]
                excess = float(tot - sector_cap)
                w[members] *= sector_cap / tot
                full = set(totals[totals >= sector_cap - 1e-9].index) | {s_name}
                room = [t for t in w.index if sec[t] not in full and w[t] < max_weight - 1e-9]
                if room:
                    w[room] += excess * w[room] / w[room].sum()
                changed = True
                totals = w.groupby(sec).sum()
        if not changed:
            break
    return w / w.sum()


def optimize_sleeve(price_df: pd.DataFrame, benchmark: pd.Series, sectors: dict, scores: dict,
                    method: str, max_weight: float = MAX_SINGLE_STOCK_WEIGHT,
                    sector_cap: float = SECTOR_CAP) -> dict:
    """Pure function (also re-used by the Backtest Agent on training data only)."""
    tickers = list(price_df.columns)
    n = len(tickers)
    notes = []
    if n == 0:
        raise ValueError("no stocks with usable price history")

    if n * max_weight < 1:
        notes.append(f"Per-stock cap raised from {max_weight:.0%} to {1 / n:.0%} — only {n} stocks available.")
        max_weight = math.ceil(1000 / n) / 1000
    sector_map = {t: sectors.get(t, "Unknown") for t in tickers}
    n_sectors = len(set(sector_map.values()))
    if n_sectors * sector_cap < 1:
        new_cap = math.ceil(1000 / n_sectors) / 1000
        notes.append(f"Sector cap raised from {sector_cap:.0%} to {new_cap:.0%} — only {n_sectors} sectors in the shortlist.")
        sector_cap = new_cap

    mu, betas = equilibrium_returns(price_df, benchmark, scores)
    S = risk_models.CovarianceShrinkage(price_df).ledoit_wolf()

    def fresh():
        ef = EfficientFrontier(mu, S, weight_bounds=(0, max_weight))
        ef.add_sector_constraints(sector_map, {}, {s: sector_cap for s in set(sector_map.values())})
        return ef

    def solve(kind: str, target=None):
        ef = fresh()
        if kind == "min_volatility":
            ef.min_volatility()
        elif kind == "max_sharpe":
            ef.max_sharpe(risk_free_rate=RISK_FREE_RATE)
        elif kind == "efficient_risk":
            ef.efficient_risk(target_volatility=target)
        return pd.Series(ef.clean_weights()).reindex(tickers).fillna(0.0)

    used = method
    try:
        if method == "hrp":
            w = apply_caps(hrp_weights(price_df), sector_map, max_weight, sector_cap)
            used = "hrp (hierarchical risk parity)"
        elif method == "min_volatility":
            w = solve("min_volatility")
        elif method == "max_sharpe":
            w = solve("max_sharpe")
        else:  # balanced: midpoint of the frontier between min-vol and max-Sharpe
            w_lo = solve("min_volatility")
            vol_lo = _perf(w_lo, mu, S)["annual_volatility"]
            try:
                w_hi = solve("max_sharpe")
                vol_hi = _perf(w_hi, mu, S)["annual_volatility"]
            except Exception:
                w_hi, vol_hi = w_lo, vol_lo
            target = vol_lo + 0.5 * (vol_hi - vol_lo)
            used = f"balanced (target volatility {target:.1%})"
            try:
                w = solve("efficient_risk", target + 1e-4) if vol_hi > vol_lo + 1e-4 else w_lo
            except Exception:
                w = 0.5 * w_lo + 0.5 * w_hi  # still feasible: constraints are convex
                used = "balanced (blend of min-vol and max-Sharpe)"
    except Exception as exc:
        notes.append(f"Optimizer could not solve ({exc.__class__.__name__}); used equal weights.")
        w = pd.Series(1.0 / n, index=tickers)
        used = "equal_weight_fallback"

    w = w.where(w > 0.005, 0.0)
    w = w / w.sum()
    weights = {t: round(float(v), 4) for t, v in w.items() if v > 0}
    return {
        "weights": weights,
        "performance": {**_perf(w, mu, S), "method": used},
        "expected_returns": {t: round(float(v), 4) for t, v in mu.items()},
        "betas": {t: round(float(b), 2) for t, b in betas.items()},
        "max_weight_used": round(max_weight, 3),
        "sector_cap_used": round(sector_cap, 3),
        "notes": notes,
        "_mu": mu, "_S": S, "_sector_map": sector_map,
    }


# ======================================================== 2c. analytics (experienced view)
def _max_feasible_return(mu: pd.Series, sector_map: dict, max_w: float, sector_cap: float) -> float:
    """Greedy fill of the highest-return stocks within stock and sector caps."""
    left, used, ret = 1.0, {}, 0.0
    for t in mu.sort_values(ascending=False).index:
        room = min(max_w, sector_cap - used.get(sector_map[t], 0.0), left)
        if room <= 0:
            continue
        ret += room * mu[t]
        used[sector_map[t]] = used.get(sector_map[t], 0.0) + room
        left -= room
        if left <= 1e-9:
            break
    return float(ret)


def sleeve_analytics(sleeve: dict, price_df: pd.DataFrame) -> dict:
    """Efficient frontier, correlations and risk contributions for the equity sleeve.

    * Efficient frontier: the best expected return available for each level of risk,
      under the same caps the optimizer used. Our portfolio should sit on it.
    * Risk contribution: share of total sleeve variance each stock is responsible for,
      w_i * (Σw)_i / wᵀΣw. A stock can be 15% of the money but 25% of the risk.
    """
    mu, S, sector_map = sleeve["_mu"], sleeve["_S"], sleeve["_sector_map"]
    max_w, sec_cap = sleeve["max_weight_used"], sleeve["sector_cap_used"]
    w = pd.Series(sleeve["weights"]).reindex(mu.index).fillna(0.0)

    def make():
        ef = EfficientFrontier(mu, S, weight_bounds=(0, max_w))
        ef.add_sector_constraints(sector_map, {}, {s: sec_cap for s in set(sector_map.values())})
        return ef

    frontier = []
    try:
        warnings.filterwarnings("ignore", message="Solution may be inaccurate")
        ef = make()
        ef.min_volatility()
        r_lo = ef.portfolio_performance()[0]
        r_hi = _max_feasible_return(mu, sector_map, max_w, sec_cap)
        for target in np.linspace(r_lo, r_hi - 1e-4, 18):
            try:
                ef = make()
                ef.efficient_return(float(target))
                ret, vol, _ = ef.portfolio_performance()
                frontier.append({"ret": round(float(ret), 4), "vol": round(float(vol), 4)})
            except Exception:
                continue
    except Exception:
        frontier = []

    var = float(w @ S @ w)
    contrib = (w * (S @ w) / var) if var > 0 else w * 0
    corr = daily_returns(price_df).corr()
    tickers = list(price_df.columns)
    return {
        "frontier": frontier,
        "assets": [{"ticker": t, "ret": round(float(mu[t]), 4), "vol": round(float(np.sqrt(S.loc[t, t])), 4)}
                   for t in tickers],
        "portfolio": {"ret": round(float(w @ mu), 4), "vol": round(float(np.sqrt(var)), 4)},
        "risk_contribution": {t: round(float(contrib[t]), 4) for t in tickers if w[t] > 0},
        "correlation": {"tickers": tickers,
                        "matrix": [[round(float(corr.loc[a, b]), 2) for b in tickers] for a in tickers]},
        "average_pairwise_correlation": round(float(
            (corr.values.sum() - len(tickers)) / max(1, len(tickers) * (len(tickers) - 1))), 3),
    }


# ======================================================== 3. blend & execute
def blend_asset_classes(bucket: dict, sleeve_weights: dict, price_df: pd.DataFrame,
                        sleeve_perf: dict, gold_history: pd.Series) -> dict:
    eq_ret, eq_vol = sleeve_perf["expected_annual_return"], sleeve_perf["annual_volatility"]
    w = pd.Series(sleeve_weights)
    eq_daily = (daily_returns(price_df[w.index]) @ w).rename("equity")
    gold_daily = daily_returns(gold_history).rename("gold")
    aligned = pd.concat([eq_daily, gold_daily], axis=1, join="inner").dropna()
    if len(aligned) >= 120:
        corr_eg = float(aligned.corr().iloc[0, 1])
        gold_vol = float(aligned["gold"].std() * np.sqrt(TRADING_DAYS))
    else:
        corr_eg, gold_vol = 0.0, GOLD_VOLATILITY_DEFAULT

    names = ["stocks", "bonds", "gold", "cash"]
    vols = np.array([eq_vol, BOND_VOLATILITY, gold_vol, CASH_VOLATILITY])
    rets = np.array([eq_ret, BOND_EXPECTED_RETURN, GOLD_EXPECTED_RETURN, CASH_EXPECTED_RETURN])
    corr = np.array([
        [1.0, CORR_EQUITY_BOND, corr_eg, 0.0],
        [CORR_EQUITY_BOND, 1.0, CORR_GOLD_BOND, 0.0],
        [corr_eg, CORR_GOLD_BOND, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])
    cov = np.outer(vols, vols) * corr
    wv = np.array([bucket[k] for k in names])
    port_vol = float(np.sqrt(wv @ cov @ wv))
    port_ret = float(wv @ rets)
    linear_vol = float(wv @ vols)
    return {
        "expected_annual_return": round(port_ret, 4),
        "annual_volatility": round(port_vol, 4),
        "sharpe_ratio": round((port_ret - RISK_FREE_RATE) / port_vol, 3) if port_vol > 0 else None,
        "volatility_if_assets_moved_in_lockstep": round(linear_vol, 4),
        "diversification_benefit": round(linear_vol - port_vol, 4),
        "equity_gold_correlation": round(corr_eg, 3),
        "gold_volatility_used": round(gold_vol, 4),
    }


def build_holdings(bucket: dict, sleeve_weights: dict, price_df: pd.DataFrame,
                   fundamentals: dict, gold: dict, investable: float) -> tuple[list, dict]:
    holdings = []
    latest = price_df.iloc[-1]
    equity_rupees = investable * bucket["stocks"]
    alloc, leftover = {}, equity_rupees
    vehicle = "direct_stocks" if equity_rupees >= MIN_DIRECT_EQUITY_RUPEES else "index_fund"
    if vehicle == "direct_stocks" and sleeve_weights:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                da = DiscreteAllocation(sleeve_weights, latest[list(sleeve_weights)],
                                        total_portfolio_value=equity_rupees)
                alloc, leftover = da.greedy_portfolio()
        except Exception:
            alloc, leftover = {}, equity_rupees
    elif vehicle == "index_fund":
        leftover = 0.0

    unbuyable = []
    for t, w in sorted(sleeve_weights.items(), key=lambda kv: -kv[1]):
        price = float(latest[t])
        target = equity_rupees * w
        shares = int(alloc.get(t, 0))
        if shares == 0 and vehicle == "direct_stocks":
            unbuyable.append({"ticker": t, "price": round(price, 2), "target_rupees": round(target, 2)})
        f = fundamentals.get(t, {})
        holdings.append({
            "asset_class": "stocks", "ticker": t, "name": f.get("name", t), "sector": f.get("sector"),
            "weight": round(w * bucket["stocks"], 4), "weight_in_sleeve": w,
            "target_rupees": round(target, 2), "price": round(price, 2),
            "shares": shares, "invested_rupees": round(shares * price, 2),
        })

    if vehicle == "index_fund" and equity_rupees > 0:
        holdings.insert(0, {"asset_class": "stocks", "ticker": None, "name": "Nifty 50 index fund (for now)",
                            "sector": "Index", "weight": bucket["stocks"], "target_rupees": round(equity_rupees, 2),
                            "invested_rupees": round(equity_rupees, 2), "is_index_fund": True})

    gold_rupees = investable * bucket["gold"]
    gold_units = int(gold_rupees // gold["price"]) if gold["price"] > 0 else 0
    holdings.append({"asset_class": "gold", "ticker": gold["ticker"], "name": gold["name"], "sector": "Gold ETF",
                     "weight": bucket["gold"], "target_rupees": round(gold_rupees, 2),
                     "price": round(gold["price"], 2), "shares": gold_units,
                     "invested_rupees": round(gold_units * gold["price"], 2)})
    holdings.append({"asset_class": "bonds", "ticker": None, "name": "Gilt / short-duration debt fund",
                     "sector": "Debt", "weight": bucket["bonds"],
                     "target_rupees": round(investable * bucket["bonds"], 2),
                     "invested_rupees": round(investable * bucket["bonds"], 2)})
    holdings.append({"asset_class": "cash", "ticker": None, "name": "Liquid fund / savings",
                     "sector": "Cash", "weight": bucket["cash"],
                     "target_rupees": round(investable * bucket["cash"], 2),
                     "invested_rupees": round(investable * bucket["cash"], 2)})

    execution = {
        "investable_amount": round(investable, 2),
        "equity_rupees": round(equity_rupees, 2),
        "equity_leftover_rupees": round(float(leftover), 2),
        "equity_leftover_share": round(float(leftover) / equity_rupees, 4) if equity_rupees > 0 else 0.0,
        "unbuyable_stocks": unbuyable,
        "equity_vehicle": vehicle,
        "min_direct_equity_rupees": MIN_DIRECT_EQUITY_RUPEES,
    }
    return holdings, execution


def vehicle_is_index(execution: dict) -> bool:
    return execution.get("equity_vehicle") == "index_fund"


# ======================================================== agent entry point
def run(profile: dict, market_data: dict, fundamentals: dict, feedback: dict | None = None,
        strategy: str = "mpt") -> dict:
    """`strategy` comes from the Strategy Selector Agent; the Validator can
    override it through feedback["strategy"] (e.g. switch to HRP when MPT keeps
    concentrating the portfolio)."""
    feedback = feedback or {}
    strategy = feedback.get("strategy", strategy)
    if strategy not in STRATEGIES:
        strategy = "mpt"
    stocks = market_data["stocks"]
    bucket, adjustments, equity_ceiling = policy_allocation(profile, feedback)

    exclude = list(feedback.get("exclude", []))
    shortlist, notes = select_shortlist(fundamentals, market_data, exclude,
                                        n=feedback.get("num_stocks", NUM_STOCKS_TO_SELECT),
                                        max_share_price=feedback.get("max_share_price"))
    price_df = pd.DataFrame({t: stocks[t]["history"] for t in shortlist}).dropna()

    method = "hrp" if strategy == "hrp" else PROFILE_METHOD[profile["risk_tolerance"]]
    if feedback.get("force_min_volatility"):
        method = "min_volatility"
    sleeve = optimize_sleeve(
        price_df, market_data["benchmark"]["history"],
        sectors={t: fundamentals[t]["sector"] for t in shortlist},
        scores={t: fundamentals[t]["fundamental_score"] for t in shortlist},
        method=method,
        max_weight=feedback.get("max_weight", MAX_SINGLE_STOCK_WEIGHT),
        sector_cap=feedback.get("sector_cap", SECTOR_CAP),
    )
    notes += sleeve["notes"]

    blended = blend_asset_classes(bucket, sleeve["weights"], price_df, sleeve["performance"],
                                  market_data["gold"]["history"])

    investable = profile["amount"] if profile["amount"] > 0 else profile["monthly_sip"]
    holdings, execution = build_holdings(bucket, sleeve["weights"], price_df, fundamentals,
                                         market_data["gold"], investable)
    execution["basis"] = "lump sum" if profile["amount"] > 0 else "first monthly SIP instalment"

    sector_exposure = {}
    for t, w in sleeve["weights"].items():
        sec = fundamentals[t]["sector"]
        sector_exposure[sec] = round(sector_exposure.get(sec, 0) + w, 4)

    final_allocation = {}
    for h in holdings:
        key = h["ticker"] or h["name"]
        if h["asset_class"] == "stocks" and vehicle_is_index(execution) and not h.get("is_index_fund"):
            continue
        final_allocation[key] = h["weight"]

    return {
        "risk_tolerance": profile["risk_tolerance"],
        "asset_class_allocation": bucket,
        "base_allocation": RISK_BUCKETS[profile["risk_tolerance"]],
        "equity_ceiling": round(equity_ceiling, 3),
        "allocation_adjustments": adjustments,
        "equity_shortlist": shortlist,
        "excluded_tickers": exclude,
        "equity_weights_within_stocks_sleeve": sleeve["weights"],
        "sector_exposure_in_sleeve": sector_exposure,
        "final_allocation": final_allocation,
        "holdings": holdings,
        "execution": execution,
        "equity_sleeve_performance": sleeve["performance"],
        "equity_sleeve_risk": portfolio_risk(sleeve["weights"], price_df, market_data["benchmark"]["history"]),
        "expected_returns_used": sleeve["expected_returns"],
        "betas": sleeve["betas"],
        "constraints_used": {"max_weight": sleeve["max_weight_used"], "sector_cap": sleeve["sector_cap_used"],
                             "method": method},
        "strategy": "hrp" if method == "hrp" else "mpt",
        "blended": blended,
        "analytics": sleeve_analytics(sleeve, price_df),
        "blended_expected_annual_return": blended["expected_annual_return"],
        "blended_annual_volatility": blended["annual_volatility"],
        "notes": notes,
        "feedback_applied": feedback,
    }
