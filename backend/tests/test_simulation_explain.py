import pytest

from agents import scenario_simulation, explainability


def test_simulation_is_deterministic():
    a = scenario_simulation.run(100000, 0, 0.10, 0.12, 10)
    b = scenario_simulation.run(100000, 0, 0.10, 0.12, 10)
    assert a["final_value_percentiles"] == b["final_value_percentiles"]


def test_sip_and_inflation():
    s = scenario_simulation.run(0, 5000, 0.10, 0.12, 10)
    assert s["total_invested"] == pytest.approx(5000 * 120)
    assert s["final_value_percentiles_real"]["p50_median"] < s["final_value_percentiles"]["p50_median"]
    assert len(s["yearly"]["years"]) == 11


def test_median_growth_is_realistic():
    s = scenario_simulation.run(100000, 0, 0.10, 0.15, 10)
    implied = (s["final_value_percentiles"]["p50_median"] / 100000) ** (1 / 10) - 1
    assert 0.07 < implied < 0.11   # median compounds a bit below the 10% mean, as it should


def test_indian_rupee_format():
    assert explainability.inr(1234567) == "₹12,34,567"
    assert explainability.inr(50000) == "₹50,000"
    assert explainability.inr(999) == "₹999"


def test_llm_number_verification():
    base = "Median outcome ₹1,96,217 after 15 years, volatility 9.2%."
    ok, _ = explainability.verify_numbers(base, "After 15 years you'd likely have ₹1,96,217 (volatility 9.2%).")
    bad, new = explainability.verify_numbers(base, "After 15 years you'd likely have ₹2,00,000.")
    assert ok and not bad and 200000.0 in new


def test_llm_rewrite_is_rejected_if_numbers_change(monkeypatch):
    """With a (fake) LLM configured, an invented figure must be caught."""
    import llm
    from workflow import run_pipeline

    monkeypatch.setattr(llm, "provider", lambda: "fake:model")
    monkeypatch.setattr(llm, "complete", lambda prompt, json_mode=False: "You could have ₹99,99,999 soon!")
    state = run_pipeline("I have ₹1,00,000. I'm 25, moderate risk, investing for 15 years.")
    exp = state["explanation"]
    assert exp["used_llm"] is False and "9999999" in exp["llm_rejected_reason"]


def test_llm_fills_gaps_rules_missed(monkeypatch):
    import llm
    from agents import user_profiling

    monkeypatch.setattr(llm, "provider", lambda: "fake:model")
    monkeypatch.setattr(llm, "complete_json", lambda prompt: {
        "amount": 75000, "risk_tolerance": "conservative", "horizon_years": 6, "age": 999})
    p = user_profiling.run("my grandmother gave me some money for when I finish college, I'd rather not lose it", {})
    assert p["amount"] == 75000 and p["field_sources"]["amount"] == "llm"
    assert p["risk_tolerance"] == "conservative"
    assert p["age"] != 999  # out-of-range LLM values are rejected
