"""
Explainability Agent
-----------------------
Turns every other agent's output into an explanation pitched at the reader.

Two audiences, same numbers:
  * beginner  — plain words, short bullet points, analogies ("four jars"),
                no jargon. "Volatility 9%" becomes "about 2 years in 3 land
                between +1% and +19%".
  * advanced  — standard investing terms: E[R], σ, Sharpe, β, VaR/CVaR,
                drawdown, CAPM, Ledoit-Wolf, efficient frontier, risk contribution.

The profiling agent detects the level from how the user writes (or the user
picks it). BOTH versions are generated every time — they're cheap templates —
so the UI can switch instead of re-running the whole graph.

Each section is structured (title / lead / points / cards) instead of one long
paragraph, so the UI can render scannable bullet points and per-stock cards.

Optional LLM: rewrites only the short "in a nutshell" overview for the
user's level, and the rewrite is VERIFIED — if it contains any number the
template didn't, it is discarded.
"""
import re

import llm
from config import MAX_PER_SECTOR_IN_SHORTLIST

DISCLAIMER = ("Educational project output only — not registered investment advice. "
              "Consult a SEBI-registered investment adviser before investing real money.")

METRIC_LABELS = {"pe": "P/E", "pb": "P/B", "roe": "ROE", "de": "Debt/Equity", "profit_margin": "Net margin"}
PLAIN_STRONG = {
    "pe": "Reasonably priced compared with its profits",
    "pb": "Cheap compared with what the company owns",
    "roe": "Makes a lot of profit from shareholders' money",
    "de": "Carries very little debt",
    "profit_margin": "Keeps a big share of its sales as profit",
}
PLAIN_WEAK = {
    "pe": "Share price is high compared with its profits",
    "pb": "Priced well above what the company owns",
    "roe": "Earns less on shareholders' money than similar companies",
    "de": "Has more debt than similar companies",
    "profit_margin": "Thinner profit margins than similar companies",
}
PLAIN_SECTOR = {
    "Technology": "IT services", "Financial Services": "Banking", "Consumer Defensive": "Everyday products",
    "Consumer Cyclical": "Cars & lifestyle", "Healthcare": "Medicines", "Industrials": "Engineering",
    "Energy": "Energy", "Communication Services": "Telecom", "Basic Materials": "Paints & materials",
}
JAR_PURPOSE = {
    "stocks": "grows the most over time, but goes up and down",
    "bonds": "steady and lower-return; cushions the falls",
    "gold": "often holds its value when stocks drop",
    "cash": "safe, ready money for emergencies and leftovers",
}


# ------------------------------------------------------------------ formatting
def inr(x: float) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567"""
    neg = x < 0
    s = f"{abs(round(x)):d}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = f"{head},{tail}"
    return ("-₹" if neg else "₹") + s


def pct(x: float, d: int = 1) -> str:
    return f"{x * 100:.{d}f}%"


def _a(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _short(t: str | None) -> str:
    return (t or "").replace(".NS", "")


def data_quality_score(market_data: dict, risk_metrics: dict) -> int:
    """How much the inputs can be trusted — NOT how good the recommendation is."""
    live = market_data["live_ratio"]
    pts = [r["data_points"] for r in risk_metrics.values()]
    richness = min(1.0, (sum(pts) / max(1, len(pts))) / 1000)
    info_live = sum(1 for s in market_data["stocks"].values() if s["info_source"] == "live") / max(1, len(market_data["stocks"]))
    return int(round(max(10, min(95, 25 + 45 * live * richness + 25 * info_live))))


def _metric_value(f: dict, m: str) -> str:
    if m == "pe":
        return f"{f['pe']:.1f}" if f.get("pe") is not None else "n/a"
    if m == "pb":
        return f"{f['pb']:.1f}" if f.get("pb") is not None else "n/a"
    if m == "roe":
        return pct(f["roe"]) if f.get("roe") is not None else "n/a"
    if m == "de":
        return f"{f['debt_to_equity']:.2f}" if f.get("debt_to_equity") is not None else "n/a"
    if m == "profit_margin":
        return pct(f["profit_margin"]) if f.get("profit_margin") is not None else "n/a"
    return "n/a"


# ------------------------------------------------------------------ stock cards
def _stock_cards(state: dict, level: str) -> list[dict]:
    opt, fund = state["optimized_portfolio"], state["fundamentals"]
    index_mode = opt["execution"].get("equity_vehicle") == "index_fund"
    contrib = opt.get("analytics", {}).get("risk_contribution", {})
    cards = []
    for h in [h for h in opt["holdings"] if h["asset_class"] == "stocks" and not h.get("is_index_fund")]:
        f = fund.get(h["ticker"], {})
        scores = {m.replace("_score", ""): v for m, v in f.get("score_breakdown", {}).items() if v is not None}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        strong = [m for m, v in ranked if v >= 65][:2] or ([ranked[0][0]] if ranked else [])
        weak = [m for m, v in reversed(ranked) if v <= 35 and m not in strong][:1]

        if index_mode:
            buy = "Target for later — use an index fund for now"
        elif h["shares"]:
            buy = f"Buy {h['shares']} share{'s' if h['shares'] != 1 else ''} · {inr(h['invested_rupees'])}"
        else:
            buy = "Can't afford a whole share yet"

        if level == "beginner":
            pros = [PLAIN_STRONG[m] for m in strong]
            cons = [PLAIN_WEAK[m] for m in weak]
            sector = PLAIN_SECTOR.get(f.get("sector"), f.get("sector"))
            metrics = {}
        else:
            within = f.get("ranked_within", "universe")
            peer = "sector" if within != "universe" else "universe"
            pros = [f"{METRIC_LABELS[m]} {_metric_value(f, m)} — {scores[m]:.0f}th pct vs {peer}" for m in strong]
            cons = [f"{METRIC_LABELS[m]} {_metric_value(f, m)} — {scores[m]:.0f}th pct vs {peer}" for m in weak]
            sector = f.get("sector")
            metrics = {
                "E[R]": pct(opt["expected_returns_used"].get(h["ticker"], 0)),
                "β": f"{opt['betas'].get(h['ticker'], 1):.2f}",
                "Risk share": pct(contrib.get(h["ticker"], 0)),
                "Score": f"{f.get('fundamental_score', 0):.0f}",
            }
        cards.append({
            "ticker": h["ticker"], "short": _short(h["ticker"]), "name": h["name"], "sector": sector,
            "weight_in_sleeve": h["weight_in_sleeve"], "weight_total": h["weight"],
            "score": f.get("fundamental_score"), "buy": buy, "pros": pros, "cons": cons, "metrics": metrics,
        })
    return cards


# ------------------------------------------------------------------ beginner
def _beginner(state: dict, dq: int) -> tuple[str, list[dict]]:
    p, opt, sim = state["user_profile"], state["optimized_portfolio"], state["simulation"]
    bt, val, md = state.get("backtest") or {}, state.get("validation") or {}, state["market_data"]
    b, bl, ex = opt["asset_class_allocation"], opt["blended"], opt["execution"]
    fv, fr = sim["final_value_percentiles"], sim["final_value_percentiles_real"]
    yrs = p["horizon_years"]
    index_mode = ex.get("equity_vehicle") == "index_fund"
    n_stocks = len(opt["equity_weights_within_stocks_sleeve"])
    sections = []

    money = []
    if p["amount"]:
        money.append(f"{inr(p['amount'])} now")
    if p["monthly_sip"]:
        money.append(f"{inr(p['monthly_sip'])} every month")
    points = [f"Investing: {' + '.join(money)}", f"For about {yrs} years",
              f"Comfort with ups and downs: {p['risk_tolerance']}", f"Age: {p['age']}"]
    points += p.get("assumptions", [])
    sections.append({"id": "profile", "title": "About you", "lead": "", "points": points})

    jars = [f"{pct(b[k], 0)} {k.title()} — {JAR_PURPOSE[k]}" for k in ("stocks", "bonds", "gold", "cash") if b[k] > 0]
    lead = "Think of your money as four jars, each with a different job."
    adj = [a.replace("Equity", "The stocks jar") for a in opt["allocation_adjustments"]]
    if adj:
        adj.append("Money you'll need soon shouldn't be in stocks — there may be no time to recover from a crash.")
    sections.append({"id": "allocation", "title": "How your money is split", "lead": lead, "points": jars + adj})

    if index_mode:
        lead = (f"Your stocks jar is {inr(ex['equity_rupees'])} right now — too small to buy single company shares "
                f"sensibly. Put it in a Nifty 50 index fund (one fund that holds India's 50 biggest companies). "
                f"When it grows, these are the companies to move into:")
    else:
        lead = (f"{n_stocks} strong companies from different industries, so one bad industry can't sink you "
                f"(at most {MAX_PER_SECTOR_IN_SHORTLIST} per industry).")
    sections.append({"id": "stocks", "title": "The companies we picked", "lead": lead, "points": [],
                     "cards": _stock_cards(state, "beginner")})
    if not index_mode and ex["equity_leftover_rupees"] > 0:
        sections[-1]["points"].append(f"{inr(ex['equity_leftover_rupees'])} is left over after buying whole shares "
                                      f"— keep it in the cash jar.")

    lo = bl["expected_annual_return"] - bl["annual_volatility"]
    hi = bl["expected_annual_return"] + bl["annual_volatility"]
    er = opt.get("equity_sleeve_risk") or {}
    points = [f"On average it might grow about {pct(bl['expected_annual_return'])} a year.",
              f"But it won't be smooth: about 2 years in 3 should land between {pct(lo, 0)} and {pct(hi, 0)}.",
              "Spreading money across the jars makes the ride smoother than stocks alone."]
    if er.get("max_drawdown"):
        points.append(f"Be ready: in the past, the stocks part fell as much as {pct(-er['max_drawdown'], 0)} "
                      f"from its high before recovering. Don't panic-sell when that happens.")
    sections.append({"id": "risk", "title": "What to expect each year", "lead": "", "points": points})

    sections.append({"id": "future", "title": f"Your money after {yrs} years", "lead":
                     f"We played out {sim['num_simulations']:,} possible futures. You'd put in {inr(sim['total_invested'])} in total.",
                     "points": [
                         f"Typical result: {inr(fv['p50_median'])} (that's like {inr(fr['p50_median'])} in today's prices, after inflation)",
                         f"Unlucky result: {inr(fv['p5'])}",
                         f"Lucky result: {inr(fv['p95'])}",
                         f"Chance of ending with less than you put in: {pct(sim['probability_of_loss'])}",
                     ]})

    if bt.get("available"):
        s, bm = bt["strategy"], bt["benchmark"]
        did = "did better than" if bt["beat_benchmark"] else "did worse than"
        points = ["We pretended it was a year ago, picked the stocks using only older data, then checked "
                  "what actually happened over the next year.",
                  f"Our picks: {pct(s['total_return'], 0)}. The market ({bm['name']}): {pct(bm['total_return'], 0)}. "
                  f"So our picks {did} the market that year.",
                  "One year is a small test — no method wins every year."]
        if md["data_source"] != "live":
            points.append("This test used demo data, so treat it as an example only.")
        sections.append({"id": "backtest", "title": "Did we test it?", "lead": "", "points": points})

    attempts = val.get("attempt", 1)
    n_checks = len(val.get("checks", []))
    points = [f"Our checker tested the plan against {n_checks} safety rules — like "
              "'don't put too much in one company' and 'make sure you can actually buy the shares'."]
    if attempts > 1:
        points.append(f"It found problems and sent the plan back {attempts - 1} time(s) to be fixed before approving it.")
    else:
        points.append("The first plan passed every rule.")
    for w in val.get("warnings", []):
        points.append(f"Still worth knowing: {w}")
    sections.append({"id": "checks", "title": "Safety checks", "lead": "", "points": points})

    if md["data_source"] != "live":
        sections.append({"id": "data", "title": "About the data", "lead": "", "points": [
            "Live market prices weren't available, so this run used demo data. The method is real; the numbers are examples."]})

    if index_mode:
        how = f"{pct(b['stocks'], 0)} in a Nifty 50 index fund for now"
    else:
        how = f"{pct(b['stocks'], 0)} in {n_stocks} companies' shares"
    overview = (f"Put {how}, {pct(b['bonds'], 0)} in bonds, {pct(b['gold'], 0)} in gold and {pct(b['cash'], 0)} in cash. "
                f"After {yrs} years, a typical outcome is {inr(fv['p50_median'])} from the {inr(sim['total_invested'])} "
                f"you put in — but expect ups and downs along the way.")
    return overview, sections


# ------------------------------------------------------------------ advanced
def _advanced(state: dict, dq: int) -> tuple[str, list[dict]]:
    p, opt, sim = state["user_profile"], state["optimized_portfolio"], state["simulation"]
    bt, val, md = state.get("backtest") or {}, state.get("validation") or {}, state["market_data"]
    b, base, bl, ex = opt["asset_class_allocation"], opt["base_allocation"], opt["blended"], opt["execution"]
    perf, cons = opt["equity_sleeve_performance"], opt["constraints_used"]
    er, an = opt.get("equity_sleeve_risk") or {}, opt.get("analytics") or {}
    fv, fr = sim["final_value_percentiles"], sim["final_value_percentiles_real"]
    yrs = p["horizon_years"]
    sections = []

    src = ", ".join(f"{k}: {v}" for k, v in p["field_sources"].items() if v not in ("none",))
    sections.append({"id": "profile", "title": "Investor profile", "lead": "", "points": [
        f"Capital {inr(p['amount'] or 0)}" + (f" + SIP {inr(p['monthly_sip'])}/mo" if p["monthly_sip"] else ""),
        f"Risk profile {p['risk_tolerance']}, horizon {yrs}y, age {p['age']}",
        f"Equity ceiling {pct(opt['equity_ceiling'], 0)} (glide path & age rule)",
        f"Field provenance — {src}",
    ] + p.get("assumptions", [])})

    mix = " / ".join(f"{pct(b[k], 0)} {k}" for k in ("stocks", "bonds", "gold", "cash"))
    base_mix = " / ".join(f"{pct(base[k], 0)}" for k in ("stocks", "bonds", "gold", "cash"))
    sections.append({"id": "allocation", "title": "Strategic asset allocation", "lead": f"{mix} (policy bucket {base_mix}).",
                     "points": opt["allocation_adjustments"] + [
                         f"Portfolio E[R] {pct(bl['expected_annual_return'])}, σ {pct(bl['annual_volatility'])}, "
                         f"Sharpe {bl['sharpe_ratio']} (rf 6.5%)",
                         f"σ via √(wᵀΣw): {pct(bl['diversification_benefit'])} below the ρ=1 linear sum "
                         f"({pct(bl['volatility_if_assets_moved_in_lockstep'])})",
                         f"Measured equity–gold ρ {bl['equity_gold_correlation']}, gold σ {pct(bl['gold_volatility_used'])}",
                     ]})

    lead = (f"{len(opt['equity_shortlist'])} names shortlisted by fundamental score (≤{MAX_PER_SECTOR_IN_SHORTLIST}/sector), "
            f"weighted by mean-variance optimization — objective: {perf['method']}.")
    points = [
        f"Expected returns: CAPM equilibrium rf + β·(E[Rm] − rf) with a ±2% fundamental tilt",
        "Covariance: Ledoit-Wolf shrinkage",
        f"Constraints: w ≤ {pct(cons['max_weight'], 0)} per name, ≤ {pct(cons['sector_cap'], 0)} per sector",
        f"Sleeve E[R] {pct(perf['expected_annual_return'])}, σ {pct(perf['annual_volatility'])}, Sharpe {perf['sharpe_ratio']}",
    ]
    if ex.get("equity_vehicle") == "index_fund":
        points.append(f"Execution: equity budget {inr(ex['equity_rupees'])} < {inr(ex['min_direct_equity_rupees'])} "
                      f"→ Nifty 50 index fund until it accumulates; weights shown are targets.")
    else:
        points.append(f"Execution: greedy discrete allocation, {inr(ex['equity_leftover_rupees'])} residual "
                      f"({pct(ex['equity_leftover_share'])} of sleeve).")
    if opt.get("excluded_tickers"):
        points.append(f"Excluded after validation (unbuyable at this budget): {', '.join(map(_short, opt['excluded_tickers']))}")
    sections.append({"id": "stocks", "title": "Equity sleeve construction", "lead": lead, "points": points,
                     "cards": _stock_cards(state, "advanced")})

    points = []
    if er:
        sleeve_rupees = ex["equity_rupees"]
        points += [
            f"Sleeve σ (historical) {pct(er['annual_volatility'])}, β vs {md['benchmark']['name']} {er.get('beta', 'n/a')}",
            f"1-day VaR95 {pct(-er['daily_var_95'])} (≈{inr(-er['daily_var_95'] * sleeve_rupees)}), "
            f"CVaR95 {pct(-er['daily_cvar_95'])} (≈{inr(-er['daily_cvar_95'] * sleeve_rupees)})",
            f"Max drawdown {pct(er['max_drawdown'])}",
        ]
    if an.get("risk_contribution"):
        top = max(an["risk_contribution"].items(), key=lambda kv: kv[1])
        w_top = opt["equity_weights_within_stocks_sleeve"].get(top[0], 0)
        points.append(f"Largest risk contributor {_short(top[0])}: {pct(top[1])} of sleeve variance on {pct(w_top)} of capital")
        points.append(f"Average pairwise correlation {an['average_pairwise_correlation']}")
    sections.append({"id": "risk", "title": "Risk metrics", "lead": "", "points": points})

    if bt.get("available"):
        s, bm, ew = bt["strategy"], bt["benchmark"], bt["equal_weight_shortlist"]
        sections.append({"id": "backtest", "title": "Out-of-sample backtest", "lead":
                         f"Trained {bt['train_period'][0]} → {bt['train_period'][1]}, tested {bt['test_period'][0]} → {bt['test_period'][1]} (buy-and-hold).",
                         "points": [
                             f"Strategy: {pct(s['total_return'])} return, σ {pct(s['annual_volatility'])}, Sharpe {s['sharpe_ratio']}, MDD {pct(s['max_drawdown'])}",
                             f"{bm['name']}: {pct(bm['total_return'])}, σ {pct(bm['annual_volatility'])}, Sharpe {bm['sharpe_ratio']}, MDD {pct(bm['max_drawdown'])}",
                             f"Equal-weight shortlist: {pct(ew['total_return'])}, Sharpe {ew['sharpe_ratio']}",
                             f"Excess return vs benchmark {pct(bt['excess_return_vs_benchmark'])}",
                         ] + bt["caveats"]})

    sections.append({"id": "future", "title": "Monte Carlo simulation", "lead":
                     f"{sim['num_simulations']:,} paths, {sim['distribution']}, μ {pct(sim['expected_annual_return'])}, "
                     f"σ {pct(sim['annual_volatility'])}, inflation {pct(sim['inflation_rate'])}.",
                     "points": [
                         f"Terminal value P5 / P50 / P95: {inr(fv['p5'])} / {inr(fv['p50_median'])} / {inr(fv['p95'])}",
                         f"Real (today's ₹) P50: {inr(fr['p50_median'])}",
                         f"P(terminal < invested {inr(sim['total_invested'])}) {pct(sim['probability_of_loss'])}; "
                         f"P(real loss) {pct(sim['probability_of_losing_to_inflation'])}",
                     ]})

    flat = state.get("validation_history_flat", [])
    fixes = list(dict.fromkeys(c["fix"] for c in flat if c.get("fix")))
    points = [f"Verdict: {val.get('verdict', '').replace('_', ' ')} after {val.get('attempt', 1)} attempt(s); "
              f"{sum(c['passed'] for c in val.get('checks', []))}/{len(val.get('checks', []))} checks passed"]
    if fixes:
        points.append("Revisions applied: " + "; ".join(fixes))
    points += val.get("warnings", [])
    sections.append({"id": "checks", "title": "Validation", "lead": "", "points": points})

    points = [f"Data quality {dq}/100 — {md['data_source'].replace('_', ' ')}, {pct(md['live_ratio'], 0)} live"]
    points += md.get("notes", []) + opt.get("notes", [])
    sections.append({"id": "data", "title": "Data provenance", "lead": "", "points": points})

    overview = (f"{pct(b['stocks'], 0)}/{pct(b['bonds'], 0)}/{pct(b['gold'], 0)}/{pct(b['cash'], 0)} stocks/bonds/gold/cash; "
                f"{len(opt['equity_weights_within_stocks_sleeve'])}-name equity sleeve via {perf['method']}. "
                f"Portfolio E[R] {pct(bl['expected_annual_return'])}, σ {pct(bl['annual_volatility'])}, Sharpe {bl['sharpe_ratio']}. "
                f"{yrs}y median terminal value {inr(fv['p50_median'])}, P(loss) {pct(sim['probability_of_loss'])}.")
    return overview, sections


# ------------------------------------------------------------------ LLM verification
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def verify_numbers(base: str, candidate: str) -> tuple[bool, list[float]]:
    new = sorted(_numbers(candidate) - _numbers(base))
    return (len(new) == 0, new)


LLM_STYLE = {
    "beginner": "for someone who has never invested before: warm, simple words, no jargon, 3 short sentences",
    "advanced": "for an experienced investor: concise and precise, using standard investing terms, 2-3 sentences",
}


def _flatten(overview: str, sections: list[dict]) -> str:
    parts = [overview]
    for s in sections:
        parts.append(s["title"])
        if s.get("lead"):
            parts.append(s["lead"])
        parts += [f"- {p}" for p in s.get("points", [])]
        for c in s.get("cards", []):
            parts.append(f"- {c['name']}: {c['buy']}. " + " ".join(c["pros"] + c["cons"]))
    return "\n".join(parts)


def run(state: dict) -> dict:
    dq = data_quality_score(state["market_data"], state["risk_metrics"])
    level = state["user_profile"].get("experience", "beginner")
    ov_b, sec_b = _beginner(state, dq)
    ov_a, sec_a = _advanced(state, dq)
    overviews = {"beginner": ov_b, "advanced": ov_a}
    levels = {"beginner": sec_b, "advanced": sec_a}

    used_llm, rejected = False, None
    if llm.provider():
        prompt = (f"Rewrite this investment summary {LLM_STYLE[level]}. Copy every number and ₹ amount exactly — "
                  f"do not add, round, or remove any figure, and do not add advice.\n\n{overviews[level]}")
        candidate = llm.complete(prompt)
        if candidate:
            candidate = candidate.strip()
            ok, new = verify_numbers(overviews[level], candidate)
            if ok:
                overviews[level], used_llm = candidate, True
            else:
                rejected = f"LLM introduced numbers not computed by the agents: {new[:5]}"

    return {
        "level": level,
        "overview": overviews,
        "levels": levels,
        "sections": levels[level],
        "summary": _flatten(overviews[level], levels[level]),
        "used_llm": used_llm,
        "llm_provider": llm.provider(),
        "llm_rejected_reason": rejected,
        "data_quality_score": dq,
        "disclaimer": DISCLAIMER,
    }
