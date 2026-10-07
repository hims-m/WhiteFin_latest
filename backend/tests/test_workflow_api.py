import json

import pytest
from fastapi.testclient import TestClient

from main import app
from workflow import run_pipeline, stream_pipeline


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def agents_run(state):
    return [t["agent"] for t in state["trace"]]


def test_clarification_gate_stops_before_market_data():
    state = run_pipeline("I want to invest some money")
    assert state["status"] == "needs_clarification"
    assert agents_run(state) == ["user_profiling", "clarify"]
    assert {q["field"] for q in state["questions"]} == {"amount", "risk_tolerance", "horizon_years"}


def test_full_run_uses_parallel_branches_and_completes():
    state = run_pipeline("I have ₹1,00,000. I'm 25, moderate risk, investing for 15 years.")
    assert state["status"] == "complete"
    order = agents_run(state)
    assert "clarify" not in order
    # fundamentals and risk both ran before the optimizer; simulation and backtest before explainability
    assert order.index("portfolio_optimization") > max(order.index("fundamental_analysis"), order.index("risk_assessment"))
    assert order.index("explainability") > max(order.index("scenario_simulation"), order.index("backtest"))
    assert state["explanation"]["sections"]


def test_validator_loop_revises_small_budget():
    state = run_pipeline("I have 20000, aggressive, 22 years old, 10 years")
    order = agents_run(state)
    attempts = order.count("portfolio_optimization")
    assert attempts == len(state["validation_history"])
    if attempts > 1:
        assert state["validation_history"][0]["verdict"] == "revise"
        assert state["optimized_portfolio"]["feedback_applied"]


def test_stream_yields_steps_then_done():
    kinds = [k for k, _ in stream_pipeline("1 lakh, conservative, 6 years")]
    assert kinds[-1] == "done" and kinds.count("step") >= 8


def test_api_analyze_and_history(client):
    r = client.post("/api/analyze", json={"prompt": "invest my money", "amount": 300000,
                                          "risk_tolerance": "moderate", "horizon_years": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "complete" and body["id"]
    item = client.get(f"/api/history/{body['id']}").json()
    assert item["optimized_portfolio"]["holdings"]
    assert any(h["id"] == body["id"] for h in client.get("/api/history").json())


def test_api_rejects_bad_input(client):
    assert client.post("/api/analyze", json={"prompt": "hi there", "age": 5}).status_code == 422
    assert client.post("/api/analyze", json={"prompt": "hi there", "risk_tolerance": "yolo"}).status_code == 422
    assert client.get("/api/history?limit=1000").status_code == 422


def test_api_stream(client):
    with client.stream("POST", "/api/analyze/stream", json={"prompt": "I want to invest"}) as s:
        events = [line.split(": ", 1)[1] for line in s.iter_lines() if line.startswith("event:")]
        assert events[-1] == "result"


def test_health_and_graph(client):
    assert client.get("/api/health").json()["offline_mode"] is True
    assert "validator" in client.get("/api/graph").json()["mermaid"]


def test_explanation_has_both_levels_and_stock_cards():
    state = run_pipeline("50k, moderate, 10y — want a good Sharpe and limited drawdown, some midcap")
    exp = state["explanation"]
    assert exp["level"] == "advanced"
    assert set(exp["levels"]) == {"beginner", "advanced"} and set(exp["overview"]) == {"beginner", "advanced"}
    for lvl in ("beginner", "advanced"):
        stocks = next(s for s in exp["levels"][lvl] if s["id"] == "stocks")
        assert stocks["cards"] and all(c["buy"] and c["pros"] for c in stocks["cards"])
    beginner_text = exp["overview"]["beginner"] + str(exp["levels"]["beginner"])
    for jargon in ("Sharpe", "σ", "VaR", "CAPM", "β"):
        assert jargon not in beginner_text
    adv_cards = next(s for s in exp["levels"]["advanced"] if s["id"] == "stocks")["cards"]
    assert "E[R]" in adv_cards[0]["metrics"]


def test_analytics_for_experienced_view():
    state = run_pipeline("I have ₹5,00,000, moderate, 12 years, age 30")
    a = state["optimized_portfolio"]["analytics"]
    assert len(a["frontier"]) >= 5
    assert abs(sum(a["risk_contribution"].values()) - 1) < 0.02
    n = len(a["correlation"]["tickers"])
    assert len(a["correlation"]["matrix"]) == n and a["correlation"]["matrix"][0][0] == 1.0


def test_api_accepts_experience(client):
    r = client.post("/api/analyze", json={"prompt": "1 lakh, moderate, 10 years", "experience": "advanced"})
    assert r.status_code == 200 and r.json()["explanation"]["level"] == "advanced"
