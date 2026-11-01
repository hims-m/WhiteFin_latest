"""
Validator Agent (critic)
--------------------------
Checks the Portfolio Optimization Agent's output against 9 hard rules. If a
rule fails, it does not just report it — it writes concrete FEEDBACK that the
optimizer applies on its next attempt (exclude a stock, tighten a cap, cut
equity). The workflow loops optimizer -> validator until everything passes or
MAX_OPTIMIZATION_ATTEMPTS is reached; then it proceeds and the remaining
issues are shown to the user as warnings.

This generate -> critique -> revise loop is the core "agentic" pattern in
WhiteFIN: a decision is made, checked, and corrected without a human.
"""
from config import (
    MAX_SINGLE_STOCK_WEIGHT, SECTOR_CAP, MIN_EFFECTIVE_HOLDINGS, RISK_VOL_CEILING,
    MAX_OPTIMIZATION_ATTEMPTS,
)

TOL = 0.005


def _prefer_hrp(feedback: dict, cons: dict) -> None:
    """Concentration problems under mean-variance usually come from noisy expected
    returns. HRP ignores return estimates and spreads risk by construction, so
    the Validator overrules the Strategy Selector and switches to it — unless the
    volatility rule has already demanded min-volatility (that fix takes priority)."""
    if cons.get("method") != "hrp" and not feedback.get("force_min_volatility"):
        feedback["strategy"] = "hrp"


def run(profile: dict, portfolio: dict, attempt: int, previous_feedback: dict | None = None,
        news: dict | None = None) -> dict:
    previous_feedback = previous_feedback or {}
    checks = []
    feedback = {k: (list(v) if isinstance(v, list) else v) for k, v in previous_feedback.items()}

    def check(name, passed, detail, fix=None):
        checks.append({"check": name, "passed": bool(passed), "detail": detail, "fix": fix if not passed else None})

    concentrated = False
    bucket = portfolio["asset_class_allocation"]
    sleeve = portfolio["equity_weights_within_stocks_sleeve"]
    cons = portfolio["constraints_used"]

    # 1. weights add up
    total = sum(bucket.values())
    sleeve_total = sum(sleeve.values())
    check("Weights sum to 100%", abs(total - 1) < 0.01 and abs(sleeve_total - 1) < 0.01,
          f"asset classes {total:.1%}, equity sleeve {sleeve_total:.1%}")

    # 2. single-stock concentration
    cap = max(cons["max_weight"], MAX_SINGLE_STOCK_WEIGHT)
    biggest = max(sleeve.items(), key=lambda kv: kv[1]) if sleeve else (None, 0)
    ok = biggest[1] <= cap + TOL
    check("No single stock above cap", ok, f"largest: {biggest[0]} at {biggest[1]:.1%} (cap {cap:.0%})",
          "tighten per-stock cap")
    if not ok:
        feedback["max_weight"] = round(max(0.2, cap - 0.05), 3)
        concentrated = True

    # 3. sector concentration
    sec_cap = max(cons["sector_cap"], SECTOR_CAP)
    exposure = portfolio["sector_exposure_in_sleeve"]
    worst_sector = max(exposure.items(), key=lambda kv: kv[1]) if exposure else (None, 0)
    ok = worst_sector[1] <= sec_cap + 0.01
    check("No sector above cap", ok, f"largest: {worst_sector[0]} at {worst_sector[1]:.1%} (cap {sec_cap:.0%})",
          "tighten sector cap")
    if not ok:
        feedback["sector_cap"] = round(max(0.25, sec_cap - 0.05), 3)

    # 4. diversification (effective number of holdings = 1 / sum(w^2))
    eff_n = 1 / sum(w * w for w in sleeve.values()) if sleeve else 0
    ok = eff_n >= MIN_EFFECTIVE_HOLDINGS - 0.05 or len(portfolio["equity_shortlist"]) < MIN_EFFECTIVE_HOLDINGS
    check("Diversified equity sleeve", ok, f"effective holdings {eff_n:.1f} (min {MIN_EFFECTIVE_HOLDINGS:.0f})",
          "lower per-stock cap to spread weight")
    if not ok:
        feedback["max_weight"] = round(max(0.2, min(cons["max_weight"], MAX_SINGLE_STOCK_WEIGHT) - 0.1), 3)
        concentrated = True

    # 5. equity share respects horizon / age ceiling
    ok = bucket["stocks"] <= portfolio["equity_ceiling"] + TOL
    check("Equity share fits horizon & age", ok,
          f"equity {bucket['stocks']:.0%}, ceiling {portfolio['equity_ceiling']:.0%}")

    # 6. total risk within the profile's volatility ceiling
    vol = portfolio["blended_annual_volatility"]
    ceiling = RISK_VOL_CEILING[profile["risk_tolerance"]]
    ok = vol <= ceiling + TOL
    check(f"Volatility within '{profile['risk_tolerance']}' limit", ok, f"{vol:.1%} vs limit {ceiling:.0%}",
          "cut equity by 10 points" + (" and switch to min-volatility" if profile["risk_tolerance"] != "aggressive" else ""))
    if not ok:
        feedback["equity_reduction"] = round(feedback.get("equity_reduction", 0) + 0.10, 2)
        if profile["risk_tolerance"] != "aggressive":
            feedback["force_min_volatility"] = True

    # 7. every chosen stock can actually be bought with this budget
    ex = portfolio["execution"]
    unbuyable = ex["unbuyable_stocks"]
    ok = not unbuyable
    if ex.get("equity_vehicle") == "index_fund":
        detail = f"equity budget under ₹{ex['min_direct_equity_rupees']:,} — index fund used instead of single stocks"
    else:
        detail = ("all stocks buyable in whole shares" if ok else
                  "; ".join(f"{u['ticker']} costs ₹{u['price']:,.0f}/share vs ₹{u['target_rupees']:,.0f} target"
                            for u in unbuyable))
    check("Every stock buyable in whole shares", ok, detail, "replace unaffordable stocks")
    if not ok:
        feedback["exclude"] = sorted(set(feedback.get("exclude", [])) | {u["ticker"] for u in unbuyable})
        # Learn the general lesson, not just this case: with this budget, a stock
        # must cost no more than an average position, or it will be unbuyable too.
        avg_position = ex["equity_rupees"] / max(3, len(portfolio["equity_shortlist"]))
        feedback["max_share_price"] = round(min(feedback.get("max_share_price", float("inf")), avg_position), 2)
        # ...and hold fewer stocks, so each position is big enough to buy.
        feedback["num_stocks"] = max(3, len(portfolio["equity_shortlist"]) - len(unbuyable))

    # 8. not too much money left idle
    leftover = ex["equity_leftover_share"]
    check("Most of the equity budget is invested", leftover <= 0.25,
          f"{leftover:.0%} of the equity budget left uninvested after whole-share rounding")

    if concentrated:
        _prefer_hrp(feedback, cons)

    # 9. no stock with a severe news red flag (fraud, regulator action, default...)
    flagged = set((news or {}).get("flagged", []))
    held_flagged = sorted(t for t in sleeve if t in flagged)
    if news is None or news.get("status") == "no_data":
        detail = "no live news available — nothing to check"
    elif held_flagged:
        per = news.get("per_ticker", {})
        detail = "; ".join(f"{t}: {per.get(t, {}).get('severe_reason') or 'red flag'}" for t in held_flagged)
    else:
        detail = f"no red flags among the {len(sleeve)} chosen stocks"
    ok = not held_flagged
    check("No stock with a severe news flag", ok, detail, "drop the flagged stocks")
    if not ok:
        feedback["exclude"] = sorted(set(feedback.get("exclude", [])) | set(held_flagged))

    failed = [c for c in checks if not c["passed"]]
    fixable = [c for c in failed if c["fix"]]
    can_retry = attempt < MAX_OPTIMIZATION_ATTEMPTS and bool(fixable) and feedback != previous_feedback

    if not failed:
        verdict = "approved"
    elif can_retry:
        verdict = "revise"
    else:
        verdict = "approved_with_warnings"

    return {
        "verdict": verdict,
        "attempt": attempt,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "feedback": feedback if verdict == "revise" else previous_feedback,
        "warnings": [f"{c['check']}: {c['detail']}" for c in failed] if verdict == "approved_with_warnings" else [],
    }
