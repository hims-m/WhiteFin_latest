"""Tests for the agents added in v2.1: HRP, Strategy Selector, News & Sentiment."""
import numpy as np
import pandas as pd
import pytest

from conftest import make_profile
from agents import news_sentiment, portfolio_optimization as po, strategy_selector, validator


# ---------------------------------------------------------------- HRP
def _toy_prices(seed=0, n=600):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    common = rng.normal(0, 0.01, n)
    vols = {"A": 0.005, "B": 0.006, "C": 0.02, "D": 0.025}
    rets = {t: common * (0.5 if t in "AB" else 1.0) + rng.normal(0, v, n) for t, v in vols.items()}
    return pd.DataFrame({t: 100 * np.cumprod(1 + r) for t, r in rets.items()}, index=idx)


def test_hrp_weights_are_valid_and_favour_low_risk():
    w = po.hrp_weights(_toy_prices())
    assert abs(w.sum() - 1) < 1e-9 and (w > 0).all()
    assert w["A"] + w["B"] > w["C"] + w["D"]   # calmer stocks get more weight


def test_apply_caps_respects_stock_and_sector_limits():
    w = pd.Series([0.7, 0.2, 0.05, 0.05], index=list("abcd"))
    capped = po.apply_caps(w, {"a": "X", "b": "X", "c": "Y", "d": "Z"}, max_weight=0.35, sector_cap=0.4)
    assert abs(capped.sum() - 1) < 1e-9
    assert capped.max() <= 0.35 + 1e-6
    assert capped["a"] + capped["b"] <= 0.4 + 1e-6


def test_optimize_sleeve_hrp_method(market_data, fundamentals):
    shortlist, _ = po.select_shortlist(fundamentals, market_data, [])
    df = pd.DataFrame({t: market_data["stocks"][t]["history"] for t in shortlist}).dropna()
    sleeve = po.optimize_sleeve(df, market_data["benchmark"]["history"],
                                {t: fundamentals[t]["sector"] for t in shortlist},
                                {t: fundamentals[t]["fundamental_score"] for t in shortlist}, "hrp")
    assert sleeve["performance"]["method"].startswith("hrp")
    assert abs(sum(sleeve["weights"].values()) - 1) < 0.01
    assert max(sleeve["weights"].values()) <= sleeve["max_weight_used"] + 0.005


def test_optimizer_uses_requested_strategy_and_feedback_override(market_data, fundamentals):
    p = make_profile()
    assert po.run(p, market_data, fundamentals, strategy="hrp")["constraints_used"]["method"] == "hrp"
    opt = po.run(p, market_data, fundamentals, feedback={"strategy": "hrp"}, strategy="mpt")
    assert opt["strategy"] == "hrp"
    assert po.run(p, market_data, fundamentals, strategy="nonsense")["strategy"] == "mpt"


# ---------------------------------------------------------------- Strategy Selector
def test_strategy_selector_bakeoff(market_data, fundamentals):
    s = strategy_selector.run(make_profile(), market_data, fundamentals)
    assert s["strategy"] in ("mpt", "hrp") and s["decided_by"] == "out_of_sample_bakeoff"
    assert set(s["scores"]) == {"mpt", "hrp"} and s["test_period"]
    a, b = s["scores"]["mpt"], s["scores"]["hrp"]
    if abs(a["sharpe_ratio"] - b["sharpe_ratio"]) > strategy_selector.SHARPE_TIE:
        best = "mpt" if a["sharpe_ratio"] > b["sharpe_ratio"] else "hrp"
        assert s["strategy"] == best


def test_strategy_selector_falls_back_on_short_history(market_data, fundamentals):
    short = {**market_data, "stocks": {t: {**v, "history": v["history"].iloc[-300:]}
                                       for t, v in market_data["stocks"].items()}}
    s = strategy_selector.run(make_profile(), short, fundamentals)
    assert s["strategy"] == "mpt" and s["decided_by"] == "fallback"


# ---------------------------------------------------------------- Validator additions
def _portfolio(market_data, fundamentals, **kw):
    return po.run(make_profile(), market_data, fundamentals, **kw)


def test_validator_drops_stock_with_severe_news(market_data, fundamentals):
    opt = _portfolio(market_data, fundamentals)
    bad = next(iter(opt["equity_weights_within_stocks_sleeve"]))
    news = {"status": "ok", "flagged": [bad],
            "per_ticker": {bad: {"severe_reason": "SEBI probe into accounting"}}}
    v = validator.run(make_profile(), opt, attempt=1, news=news)
    check = next(c for c in v["checks"] if c["check"] == "No stock with a severe news flag")
    assert not check["passed"] and "SEBI" in check["detail"]
    assert v["verdict"] == "revise" and bad in v["feedback"]["exclude"]


def test_validator_has_nine_checks_and_passes_without_news(market_data, fundamentals):
    v = validator.run(make_profile(), _portfolio(market_data, fundamentals), attempt=1, news=None)
    assert len(v["checks"]) == 9
    assert next(c for c in v["checks"] if "news" in c["check"])["passed"]


def test_validator_switches_concentrated_mpt_to_hrp(market_data, fundamentals):
    opt = _portfolio(market_data, fundamentals)
    first = next(iter(opt["equity_weights_within_stocks_sleeve"]))
    rest = [t for t in opt["equity_weights_within_stocks_sleeve"] if t != first]
    opt["equity_weights_within_stocks_sleeve"] = {first: 0.8, **{t: 0.2 / len(rest) for t in rest}}
    opt["constraints_used"]["method"] = "balanced"
    v = validator.run(make_profile(), opt, attempt=1)
    assert v["verdict"] == "revise" and v["feedback"]["strategy"] == "hrp"


# ---------------------------------------------------------------- News & Sentiment
def test_parses_both_yfinance_news_shapes():
    old = {"title": "TCS wins large deal", "publisher": "ET", "link": "https://x", "providerPublishTime": 1730000000}
    new = {"id": "1", "content": {"title": "Infosys beats estimates", "pubDate": "2026-10-30T10:00:00Z",
                                  "provider": {"displayName": "Mint"}, "canonicalUrl": {"url": "https://y"}}}
    a, b = news_sentiment._parse_yf_item(old), news_sentiment._parse_yf_item(new)
    assert a["title"] == "TCS wins large deal" and a["publisher"] == "ET" and a["published"]
    assert b["title"] == "Infosys beats estimates" and b["publisher"] == "Mint" and b["published"] == "2026-10-30"
    assert news_sentiment._parse_yf_item({"content": {}}) is None


@pytest.mark.parametrize("headline, severe", [
    ("SEBI orders probe into XYZ accounting", True),
    ("Company X files for insolvency at NCLT", True),
    ("ED raids promoter offices", True),
    ("Q2 profit falls 5% on weak demand", False),
    ("Shares rally after strong results", False),
])
def test_severe_patterns(headline, severe):
    assert bool(news_sentiment.severe_match(headline)) is severe


def test_lexicon_sentiment_direction():
    assert news_sentiment.lexicon_score(["Profit jumps, shares surge to record high"]) > 0
    assert news_sentiment.lexicon_score(["Shares plunge after downgrade and weak results"]) < 0
    assert news_sentiment.lexicon_score(["Company holds annual meeting"]) == 0


def test_llm_red_flag_must_cite_a_real_headline(monkeypatch):
    import llm
    heads = {"AAA.NS": [{"title": "AAA opens new plant"}], "BBB.NS": [{"title": "BBB under SEBI probe for fraud"}]}
    monkeypatch.setattr(llm, "provider", lambda: "fake:model")
    monkeypatch.setattr(llm, "complete_json", lambda prompt: {
        "AAA.NS": {"sentiment": 0.6, "severe": True, "severe_headline": 7},   # invented index -> ignored
        "BBB.NS": {"sentiment": 0.9, "severe": False}})                       # LLM misses it -> regex catches it
    out = news_sentiment.classify_llm(heads, {})
    assert out["AAA.NS"]["severe"] is False and out["AAA.NS"]["sentiment"] == 0.6
    assert out["BBB.NS"]["severe"] is True


def test_offline_returns_no_data(market_data):
    n = news_sentiment.run(market_data)
    assert n["status"] == "no_data" and n["flagged"] == []


# ---------------------------------------------------------------- end to end
def test_pipeline_runs_new_agents_and_drops_flagged_stock(monkeypatch):
    import workflow

    # Flag whichever stock the optimizer picks first, then check the critic loop removes it.
    first = {}
    real_opt = po.run

    def spy_opt(*a, **k):
        res = real_opt(*a, **k)
        first.setdefault("pick", next(iter(res["equity_weights_within_stocks_sleeve"])))
        return res

    monkeypatch.setattr(po, "run", spy_opt)
    real_validator = validator.run

    def validator_with_flag(profile, opt, attempt, fb=None, news=None):
        news = {"status": "ok", "flagged": [first["pick"]],
                "per_ticker": {first["pick"]: {"severe_reason": "fraud allegations (test)"}}}
        return real_validator(profile, opt, attempt, fb, news=news)

    monkeypatch.setattr(validator, "run", validator_with_flag)

    state = workflow.run_pipeline("I have ₹1,00,000. I'm 25, moderate risk, investing for 15 years.")
    agents = [t["agent"] for t in state["trace"]]
    assert "news_sentiment" in agents and "strategy_selector" in agents
    assert agents.index("strategy_selector") < agents.index("portfolio_optimization")
    assert state["attempt"] >= 2
    assert first["pick"] not in state["optimized_portfolio"]["equity_weights_within_stocks_sleeve"]
    sections = [s["id"] for s in state["explanation"]["levels"]["advanced"]]
    assert "news" in sections


def test_api_returns_strategy_and_news():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        r = c.post("/api/analyze", json={"prompt": "1 lakh, moderate, 10 years, age 30"}).json()
    assert r["strategy"]["strategy"] in ("mpt", "hrp")
    assert r["news"]["status"] == "no_data"
    assert r["optimized_portfolio"]["strategy"] in ("mpt", "hrp")
