"""
Strategy Selector Agent
--------------------------
Decides HOW the equity sleeve is built for this request, from evidence
instead of a fixed rule:

  * MPT — mean-variance optimisation (min-vol / balanced / max-Sharpe by profile).
    Uses expected returns, so it can concentrate when those estimates are noisy.
  * HRP — Hierarchical Risk Parity. Uses only the covariance structure, so it is
    usually steadier and more diversified, but ignores return views.

Method — a small out-of-sample bake-off:
  1. Take the shortlist the optimizer would use.
  2. Hide the most recent BACKTEST_TEST_DAYS (~1 year) of prices.
  3. Build BOTH portfolios from the older data only, hold each through the
     hidden year, and score them.
  4. Pick the higher Sharpe ratio. If the two are within SHARPE_TIE (0.05),
     pick the one with the smaller maximum drawdown (the less painful ride).

The choice is not final: if MPT keeps failing the Validator's concentration
checks, the Validator switches the optimizer to HRP through its feedback.
Any error -> falls back to "mpt" (the original behaviour), with the reason.
"""
import pandas as pd

from config import BACKTEST_TEST_DAYS
from agents.portfolio_optimization import PROFILE_METHOD, optimize_sleeve, select_shortlist
from agents.backtest import _buy_and_hold, _metrics

SHARPE_TIE = 0.05
MIN_TRAIN_DAYS = 250


def _fallback(reason: str) -> dict:
    return {"strategy": "mpt", "reason": reason, "scores": {}, "test_period": None, "decided_by": "fallback"}


def run(profile: dict, market_data: dict, fundamentals: dict, exclude: list[str] | None = None) -> dict:
    try:
        stocks = market_data["stocks"]
        shortlist, _ = select_shortlist(fundamentals, market_data, list(exclude or []))
        prices = pd.DataFrame({t: stocks[t]["history"] for t in shortlist}).dropna()
        if len(prices) < BACKTEST_TEST_DAYS + MIN_TRAIN_DAYS:
            return _fallback(f"Not enough shared price history for a fair test ({len(prices)} days); "
                             "kept the standard method.")

        train = prices.iloc[:-BACKTEST_TEST_DAYS]
        test = prices.iloc[-BACKTEST_TEST_DAYS - 1:]
        bench = market_data["benchmark"]["history"]
        bench_train = bench[bench.index <= train.index[-1]]
        sectors = {t: fundamentals[t]["sector"] for t in shortlist}
        scores_in = {t: fundamentals[t]["fundamental_score"] for t in shortlist}

        mpt_method = PROFILE_METHOD[profile["risk_tolerance"]]
        results = {}
        for name, method in (("mpt", mpt_method), ("hrp", "hrp")):
            sleeve = optimize_sleeve(train, bench_train, sectors, scores_in, method)
            m = _metrics(_buy_and_hold(test, sleeve["weights"]))
            results[name] = {
                "method": sleeve["performance"]["method"],
                "sharpe_ratio": m["sharpe_ratio"],
                "total_return": m["total_return"],
                "annual_volatility": m["annual_volatility"],
                "max_drawdown": m["max_drawdown"],
                "largest_weight": round(max(sleeve["weights"].values()), 4),
            }

        s_mpt = results["mpt"]["sharpe_ratio"] if results["mpt"]["sharpe_ratio"] is not None else -9
        s_hrp = results["hrp"]["sharpe_ratio"] if results["hrp"]["sharpe_ratio"] is not None else -9
        if abs(s_mpt - s_hrp) <= SHARPE_TIE:
            # drawdowns are negative numbers: the larger one is the smaller loss
            choice = "hrp" if results["hrp"]["max_drawdown"] > results["mpt"]["max_drawdown"] else "mpt"
            reason = (f"Sharpe ratios were within {SHARPE_TIE} ({s_mpt:.2f} vs {s_hrp:.2f}), so the method "
                      f"with the smaller worst drop won ({results[choice]['max_drawdown']:.1%}).")
        else:
            choice = "mpt" if s_mpt > s_hrp else "hrp"
            reason = (f"{choice.upper()} had the better risk-adjusted return on the hidden year "
                      f"(Sharpe {max(s_mpt, s_hrp):.2f} vs {min(s_mpt, s_hrp):.2f}).")

        caveats = []
        if market_data.get("data_source") != "live":
            caveats.append("Some prices are synthetic demo data, so this choice is illustrative.")
        return {
            "strategy": choice,
            "reason": reason,
            "scores": results,
            "test_period": [test.index[0].strftime("%Y-%m-%d"), test.index[-1].strftime("%Y-%m-%d")],
            "shortlist_tested": shortlist,
            "decided_by": "out_of_sample_bakeoff",
            "caveats": caveats,
        }
    except Exception as exc:  # never block the pipeline on the selector
        return _fallback(f"Strategy test failed ({exc.__class__.__name__}); kept the standard method.")
