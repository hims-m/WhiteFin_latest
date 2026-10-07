import numpy as np
import pandas as pd

import database
from agents import market_intelligence, fundamental_analysis


def test_synthetic_data_is_reproducible(market_data):
    again = market_intelligence.run()
    a = market_data["stocks"]["TCS.NS"]["history"]
    b = again["stocks"]["TCS.NS"]["history"]
    assert np.allclose(a.values, b.values)  # v1 used hash(), which changes every process


def test_offline_labels(market_data):
    assert market_data["data_source"] == "offline_fallback"
    assert market_data["live_ratio"] == 0
    assert all(s["history"].index.tz is None for s in market_data["stocks"].values())


def test_live_path_with_mocked_yfinance(monkeypatch):
    """Exercise the live branch without the internet: tz-aware prices, % debt/equity,
    one ticker missing -> 'mixed'."""
    import yfinance as yf

    idx = pd.date_range("2021-01-01", periods=600, freq="B", tz="Asia/Kolkata")
    tickers = market_intelligence.STOCK_UNIVERSE
    present = tickers[:-1] + [market_intelligence.GOLD_TICKER, market_intelligence.BENCHMARK_TICKER]
    rng = np.random.default_rng(0)
    close = pd.DataFrame({t: 100 * np.cumprod(1 + rng.normal(0.0004, 0.015, len(idx))) for t in present}, index=idx)
    frame = pd.concat({"Close": close}, axis=1)

    class FakeTicker:
        def __init__(self, t):
            self.info = {"trailingPE": 20.0, "priceToBook": 3.0, "returnOnEquity": 0.18, "debtToEquity": 45.0,
                         "profitMargins": 0.12, "dividendYield": 0.01, "sector": "Technology", "shortName": t}

    monkeypatch.setattr(yf, "download", lambda *a, **k: frame)
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    monkeypatch.setattr(market_intelligence, "OFFLINE_MODE", False)
    monkeypatch.setattr(database, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(database, "cache_set", lambda *a, **k: None)

    md = market_intelligence.run()
    assert md["data_source"] == "mixed"
    assert md["live_ratio"] == round((len(tickers) - 1) / len(tickers), 3)
    live = md["stocks"][tickers[0]]
    assert live["source"] == "live" and live["de"] == 0.45          # 45% -> 0.45
    assert live["history"].index.tz is None                          # tz stripped
    assert md["stocks"][tickers[-1]]["source"] == "offline_fallback"
    assert md["benchmark"]["source"] == "live"


def test_banks_ignore_debt_to_equity(fundamentals):
    banks = [v for v in fundamentals.values() if v["is_financial"]]
    assert banks and all(b["debt_to_equity"] is None for b in banks)
    assert all("de_score" not in b["score_breakdown"] for b in banks)


def test_scores_in_range(fundamentals):
    assert all(0 <= v["fundamental_score"] <= 100 for v in fundamentals.values())


def test_outlier_does_not_squash_scores(market_data):
    md = {**market_data, "stocks": {t: dict(s) for t, s in market_data["stocks"].items()}}
    md["stocks"]["TITAN.NS"]["pe"] = 5000  # absurd outlier
    f = fundamental_analysis.run(md)
    pe_scores = sorted(v["score_breakdown"]["pe_score"] for v in f.values()
                       if v["score_breakdown"].get("pe_score") is not None)
    assert np.std(pe_scores) > 20  # ranks stay spread out


def test_loss_making_company_is_not_cheapest(market_data):
    md = {**market_data, "stocks": {t: dict(s) for t, s in market_data["stocks"].items()}}
    md["stocks"]["RELIANCE.NS"]["pe"] = -15
    f = fundamental_analysis.run(md)
    assert f["RELIANCE.NS"]["loss_making"]
    assert f["RELIANCE.NS"]["score_breakdown"]["pe_score"] == 0
