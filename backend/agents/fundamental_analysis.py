"""
Fundamental Analysis Agent
----------------------------
Scores every stock 0-100 on valuation and quality so the optimizer has a
principled shortlist and a small "view" on expected returns.

What changed vs. v1 (and why):
  * Percentile ranks instead of min-max scaling. With min-max, one outlier
    (Titan's P/E of 68) squashes every other stock into a narrow band.
    Ranks only care about order, so outliers can't distort the scale.
  * Sector-relative ranking when a sector has >= 3 members. A P/E of 50 is
    normal for FMCG and expensive for a bank; comparing within the sector is
    fairer. Small sectors are ranked against the whole universe.
  * Banks/financials are scored differently: Debt/Equity is meaningless for a
    bank (customer deposits are liabilities), so it is dropped and P/B and ROE
    — the metrics bank analysts actually use — carry more weight.
  * Negative or missing P/E (a loss-making company) can no longer score as
    "cheapest" — it gets the worst valuation score.
  * Missing metrics are given a neutral 50 and listed in `missing_metrics`
    so the explanation can say so, instead of silently inventing a value.
"""
import pandas as pd

MIN_SECTOR_SIZE = 3

# (metric, higher_is_better)
METRICS = {
    "pe": False,
    "pb": False,
    "roe": True,
    "de": False,
    "profit_margin": True,
}
WEIGHTS_GENERAL = {"pe": 0.25, "pb": 0.15, "roe": 0.30, "de": 0.15, "profit_margin": 0.15}
WEIGHTS_FINANCIAL = {"pe": 0.30, "pb": 0.30, "roe": 0.40}


def _rank_scores(values: pd.Series, higher_is_better: bool) -> pd.Series:
    """Percentile rank 0-100 among non-missing values."""
    valid = values.dropna()
    if len(valid) <= 1:
        return pd.Series(50.0, index=values.index).where(values.notna())
    ranks = valid.rank(pct=True, ascending=higher_is_better)
    # rescale so the worst is 0 and the best is 100
    lo, hi = ranks.min(), ranks.max()
    scaled = (ranks - lo) / (hi - lo) * 100 if hi > lo else ranks * 0 + 50
    return scaled.reindex(values.index)


def run(market_data: dict) -> dict:
    stocks = market_data["stocks"]
    df = pd.DataFrame({
        t: {
            "sector": s["sector"],
            "is_financial": s["is_financial"],
            "pe": s["pe"],
            "pb": s["pb"],
            "roe": s["roe"],
            "de": s["de"],
            "profit_margin": s["profit_margin"],
        }
        for t, s in stocks.items()
    }).T
    for col in METRICS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Loss-making companies: P/E is meaningless/negative -> treat as most expensive
    loss_making = df["pe"].notna() & (df["pe"] <= 0)
    df.loc[loss_making, "pe"] = df["pe"].max(skipna=True) * 2 if df["pe"].notna().any() else None
    # Debt/Equity does not apply to banks
    df.loc[df["is_financial"].astype(bool), "de"] = None

    scores = pd.DataFrame(index=df.index, columns=list(METRICS), dtype=float)
    sector_sizes = df["sector"].value_counts()
    for metric, higher in METRICS.items():
        universe = _rank_scores(df[metric], higher)
        scores[metric] = universe
        for sector, size in sector_sizes.items():
            if size >= MIN_SECTOR_SIZE:
                members = df.index[df["sector"] == sector]
                within = _rank_scores(df.loc[members, metric], higher)
                scores.loc[members, metric] = within

    results = {}
    for ticker, row in df.iterrows():
        s = stocks[ticker]
        weights = WEIGHTS_FINANCIAL if s["is_financial"] else WEIGHTS_GENERAL
        missing = [m for m in weights if pd.isna(scores.loc[ticker, m])]
        composite = sum(
            w * (50.0 if pd.isna(scores.loc[ticker, m]) else float(scores.loc[ticker, m]))
            for m, w in weights.items()
        )
        breakdown = {f"{m}_score": (None if pd.isna(scores.loc[ticker, m]) else round(float(scores.loc[ticker, m]), 1))
                     for m in weights}
        best = max((m for m in weights if m not in missing), key=lambda m: scores.loc[ticker, m], default=None)
        worst = min((m for m in weights if m not in missing), key=lambda m: scores.loc[ticker, m], default=None)

        def _r(x, n):
            return None if x is None or pd.isna(x) else round(float(x), n)

        results[ticker] = {
            "name": s["name"],
            "sector": s["sector"],
            "is_financial": bool(s["is_financial"]),
            "pe": _r(s["pe"], 2),
            "pb": _r(s["pb"], 2),
            "roe": _r(s["roe"], 4),
            "debt_to_equity": None if s["is_financial"] else _r(s["de"], 2),
            "profit_margin": _r(s["profit_margin"], 4),
            "loss_making": bool(loss_making.get(ticker, False)),
            "ranked_within": s["sector"] if sector_sizes.get(s["sector"], 0) >= MIN_SECTOR_SIZE else "universe",
            "score_breakdown": breakdown,
            "strongest_metric": best,
            "weakest_metric": worst,
            "missing_metrics": missing,
            "fundamental_score": round(composite, 1),
            "data_source": s.get("info_source", "fallback"),
        }
    return results
