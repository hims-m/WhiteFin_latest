"""Test setup: force offline synthetic data and a throwaway database BEFORE
any project module (which reads config at import time) is imported."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["WHITEFIN_OFFLINE"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("OLLAMA_MODEL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import database  # noqa: E402

database.init_db()


@pytest.fixture(scope="session")
def market_data():
    from agents import market_intelligence
    return market_intelligence.run()


@pytest.fixture(scope="session")
def fundamentals(market_data):
    from agents import fundamental_analysis
    return fundamental_analysis.run(market_data)


def make_profile(**kw):
    base = {"amount": 100000.0, "monthly_sip": 0.0, "age": 25, "risk_tolerance": "moderate",
            "horizon_years": 15, "goal": "wealth growth"}
    base.update(kw)
    return base
