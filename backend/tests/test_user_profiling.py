import pytest

from agents.user_profiling import run


@pytest.mark.parametrize("prompt, expected", [
    # the README's own example — v1 lost the age here and silently used 30
    ("I have ₹50,000, I'm 20, moderate risk, long-term growth",
     {"amount": 50000, "age": 20, "risk_tolerance": "moderate", "horizon_years": 15}),
    ("I have ₹1,00,000 to invest. I'm 25 years old, moderate risk, over the next 15 years.",
     {"amount": 100000, "age": 25, "horizon_years": 15}),
    ("1.5 lakh for 10 years, conservative", {"amount": 150000, "horizon_years": 10}),
    ("50k, aggressive, 20 yrs", {"amount": 50000, "horizon_years": 20, "risk_tolerance": "aggressive"}),
    ("2 crore, balanced, 12 years", {"amount": 20_000_000, "risk_tolerance": "moderate"}),
    ("SIP of 5000 per month, aggressive, I am 22, retire at 60",
     {"monthly_sip": 5000, "amount": 0, "age": 22, "horizon_years": 38, "goal": "retirement"}),
    ("I can put 5k monthly and 2 lakh now, moderate, for 7 years", {"monthly_sip": 5000, "amount": 200000}),
    ("Aged 19, ₹10,000, moderate, need it in 18 months", {"age": 19, "horizon_years": 2}),
])
def test_extraction(prompt, expected):
    p = run(prompt, {})
    for k, v in expected.items():
        assert p[k] == v, (k, p[k], v)


@pytest.mark.parametrize("prompt, risk", [
    ("I don't want high risk. 1 lakh for 5 years", "conservative"),   # v1 returned aggressive
    ("not too risky please, 1 lakh, 5 years", "moderate"),
    ("I don't mind high risk, 1 lakh, 5 years", "aggressive"),
    ("no risk at all, 1 lakh, 5 years", "conservative"),
    ("moderate to aggressive, 1 lakh, 5 years", "moderate"),           # picks the more cautious
    ("I'm not conservative, 1 lakh, 5 years", "moderate"),
])
def test_risk_negation(prompt, risk):
    assert run(prompt, {})["risk_tolerance"] == risk


def test_missing_fields_trigger_clarification():
    p = run("I want to invest some money", {})
    assert set(p["missing_required"]) == {"amount", "risk_tolerance", "horizon_years"}
    assert p["amount"] is None


def test_use_defaults_reports_assumptions():
    p = run("I want to invest some money", {}, use_defaults=True)
    assert p["missing_required"] == []
    assert p["field_sources"]["amount"] == "default"
    assert len(p["assumptions"]) >= 3


def test_form_overrides_win():
    p = run("I have 50000, aggressive, 10 years", {"amount": 75000, "risk_tolerance": "conservative"})
    assert p["amount"] == 75000 and p["risk_tolerance"] == "conservative"
    assert p["field_sources"]["amount"] == "form"


def test_car_does_not_match_career():
    p = run("Starting my career, 1 lakh, moderate, 10 years", {})
    assert p["goal"] != "short-term goal"


@pytest.mark.parametrize("prompt, level", [
    ("I have 50k, moderate, 10 years", "beginner"),                       # default
    ("I'm new to investing, 50k, moderate, 10 years", "beginner"),
    ("50k, moderate, 10y — want a good Sharpe and limited drawdown, some midcap", "advanced"),
])
def test_experience_detection(prompt, level):
    assert run(prompt, {})["experience"] == level


def test_experience_override():
    p = run("I'm new to investing, 50k, moderate, 10 years", {"experience": "advanced"})
    assert p["experience"] == "advanced" and p["field_sources"]["experience"] == "form"


def test_short_year_suffix():
    assert run("50k, moderate, 10y", {})["horizon_years"] == 10
