import pandas as pd
import pytest

from agents import portfolio_optimization as po, validator, risk_assessment
from config import SECTOR_CAP, MAX_SINGLE_STOCK_WEIGHT
from conftest import make_profile


def test_glide_path_caps_equity_for_short_horizon():
    bucket, adj, ceiling = po.policy_allocation(make_profile(risk_tolerance="aggressive", horizon_years=2), {})
    assert bucket["stocks"] == pytest.approx(0.20)
    assert abs(sum(bucket.values()) - 1) < 1e-6
    assert adj and "2-year horizon" in adj[0]


def test_age_rule_caps_equity():
    bucket, adj, _ = po.policy_allocation(make_profile(age=60, horizon_years=20), {})
    assert bucket["stocks"] == pytest.approx(0.50)
    assert "age" in adj[0]


def test_portfolio_respects_constraints(market_data, fundamentals):
    opt = po.run(make_profile(), market_data, fundamentals)
    w = opt["equity_weights_within_stocks_sleeve"]
    assert abs(sum(w.values()) - 1) < 0.01
    assert max(w.values()) <= MAX_SINGLE_STOCK_WEIGHT + 0.005
    assert max(opt["sector_exposure_in_sleeve"].values()) <= SECTOR_CAP + 0.01
    assert abs(sum(opt["asset_class_allocation"].values()) - 1) < 1e-6


def test_covariance_aware_volatility_is_below_linear_sum(market_data, fundamentals):
    opt = po.run(make_profile(), market_data, fundamentals)
    b = opt["blended"]
    assert b["annual_volatility"] < b["volatility_if_assets_moved_in_lockstep"]


def test_expected_returns_are_sane(market_data, fundamentals):
    opt = po.run(make_profile(risk_tolerance="aggressive"), market_data, fundamentals)
    for r in opt["expected_returns_used"].values():
        assert 0.0 < r < 0.30   # equilibrium returns, not extrapolated bull runs


def test_infeasible_cap_is_relaxed(market_data):
    stocks = market_data["stocks"]
    prices = pd.DataFrame({t: stocks[t]["history"] for t in ["TCS.NS", "ITC.NS"]}).dropna()
    out = po.optimize_sleeve(prices, market_data["benchmark"]["history"], {"TCS.NS": "A", "ITC.NS": "B"},
                             {}, "min_volatility")
    assert abs(sum(out["weights"].values()) - 1) < 0.01
    assert out["max_weight_used"] >= 0.5 and out["notes"]


def test_whole_share_execution(market_data, fundamentals):
    opt = po.run(make_profile(amount=500000), market_data, fundamentals)
    for h in opt["holdings"]:
        if h["asset_class"] == "stocks":
            assert isinstance(h["shares"], int)
            assert h["invested_rupees"] == pytest.approx(h["shares"] * h["price"], rel=1e-6)


def test_tiny_budget_uses_index_fund(market_data, fundamentals):
    opt = po.run(make_profile(amount=0, monthly_sip=2000), market_data, fundamentals)
    assert opt["execution"]["equity_vehicle"] == "index_fund"
    assert any(h.get("is_index_fund") for h in opt["holdings"])


def test_validator_sends_back_unbuyable_portfolio(market_data, fundamentals):
    profile = make_profile(amount=20000, risk_tolerance="aggressive", horizon_years=10, age=22)
    opt = po.run(profile, market_data, fundamentals)
    v = validator.run(profile, opt, attempt=1)
    if opt["execution"]["unbuyable_stocks"]:
        assert v["verdict"] == "revise"
        assert v["feedback"]["exclude"] and v["feedback"]["max_share_price"] > 0
        revised = po.run(profile, market_data, fundamentals, v["feedback"])
        assert not set(v["feedback"]["exclude"]) & set(revised["equity_shortlist"])


def test_validator_approves_clean_portfolio(market_data, fundamentals):
    profile = make_profile(amount=1_000_000)
    v = validator.run(profile, po.run(profile, market_data, fundamentals), attempt=1)
    assert v["verdict"] == "approved", v["failed"]


def test_validator_stops_after_max_attempts(market_data, fundamentals):
    profile = make_profile(amount=20000, risk_tolerance="aggressive")
    opt = po.run(profile, market_data, fundamentals)
    opt["execution"]["unbuyable_stocks"] = [{"ticker": "X", "price": 1e6, "target_rupees": 1}]
    v = validator.run(profile, opt, attempt=3)
    assert v["verdict"] == "approved_with_warnings" and v["warnings"]


def test_cvar_is_worse_than_var(market_data):
    r = risk_assessment.run(market_data)["TCS.NS"]
    assert r["daily_cvar_95"] <= r["daily_var_95"] < 0
