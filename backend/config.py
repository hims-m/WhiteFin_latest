"""
config.py
----------
Central configuration. Values come from environment variables / a local .env
file (python-dotenv) and fall back to safe defaults, so the app runs with zero
configuration.

Every financial assumption lives here (not hard-coded inside agents) so it is
visible, documented, and easy to change for experiments.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list_from_env(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    return [t.strip() for t in raw.split(",") if t.strip()]


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------
BACKEND_PORT: int = int(_env("BACKEND_PORT", "8000"))
DATABASE_URL: str = _env("DATABASE_URL", "sqlite:///./whitefin.db")
CORS_ORIGINS: list[str] = _list_from_env(
    "CORS_ORIGINS",
    ["http://127.0.0.1:5500", "http://localhost:5500", "http://127.0.0.1:8000", "http://localhost:8000", "null"],
)

# Force the offline (synthetic) data path — used by tests/CI and for demos
# without internet. Never set this in a "real" run.
OFFLINE_MODE: bool = _env_bool("WHITEFIN_OFFLINE", False)

# How long fetched market data stays fresh in the SQLite cache.
MARKET_CACHE_TTL_HOURS: float = _env_float("MARKET_CACHE_TTL_HOURS", 6)

# --------------------------------------------------------------------------
# Optional LLMs (the app works fully without either)
# --------------------------------------------------------------------------
# Option A — fully local & free: install Ollama (https://ollama.com), run
# `ollama pull llama3.2`, then set OLLAMA_MODEL=llama3.2
OLLAMA_URL: str = _env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = _env("OLLAMA_MODEL", "")
# Option B — Google Gemini free tier. Model names get retired over time, so
# the model is configurable; check https://ai.google.dev/gemini-api/docs/models
GEMINI_API_KEY: str = _env("GEMINI_API_KEY", "")
GEMINI_MODEL: str = _env("GEMINI_MODEL", "gemini-2.5-flash")
LLM_TIMEOUT_SECONDS: float = _env_float("LLM_TIMEOUT_SECONDS", 30)

# --------------------------------------------------------------------------
# Market universe
# --------------------------------------------------------------------------
DEFAULT_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "KOTAKBANK.NS", "LT.NS", "SBIN.NS",
    "BHARTIARTL.NS", "ASIANPAINT.NS", "MARUTI.NS", "TITAN.NS", "SUNPHARMA.NS",
]
STOCK_UNIVERSE: list[str] = _list_from_env("STOCK_UNIVERSE", DEFAULT_UNIVERSE)
GOLD_TICKER: str = _env("GOLD_TICKER", "GOLDBEES.NS")
BENCHMARK_TICKER: str = _env("BENCHMARK_TICKER", "^NSEI")   # Nifty 50 index
HISTORY_PERIOD: str = _env("HISTORY_PERIOD", "5y")           # was 2y — too short for return estimates
MIN_HISTORY_POINTS = 250                                     # ~1 trading year minimum to trust a series

# --------------------------------------------------------------------------
# Capital-market assumptions (long-run, India). These replace "whatever the
# last 2 years happened to do", which made projections explode in bull runs.
# --------------------------------------------------------------------------
RISK_FREE_RATE = _env_float("RISK_FREE_RATE", 0.065)            # ~10y G-Sec yield
MARKET_EXPECTED_RETURN = _env_float("MARKET_EXPECTED_RETURN", 0.12)  # long-run Nifty assumption
FUNDAMENTAL_TILT = 0.02        # max ±2% p.a. return "view" from fundamental score (see optimizer)
INFLATION_RATE = _env_float("INFLATION_RATE", 0.055)

BOND_EXPECTED_RETURN = 0.07
BOND_VOLATILITY = 0.03
CASH_EXPECTED_RETURN = 0.035
CASH_VOLATILITY = 0.005
GOLD_EXPECTED_RETURN = 0.08
GOLD_VOLATILITY_DEFAULT = 0.15   # used only if no usable gold history
# Assumed correlations for asset classes we don't have price histories for.
CORR_EQUITY_BOND = 0.10
CORR_GOLD_BOND = 0.05
# (cash is treated as uncorrelated; equity-gold correlation is measured from data)

# --------------------------------------------------------------------------
# Allocation policy
# --------------------------------------------------------------------------
RISK_BUCKETS = {
    "conservative": {"stocks": 0.30, "bonds": 0.50, "gold": 0.10, "cash": 0.10},
    "moderate":     {"stocks": 0.60, "bonds": 0.25, "gold": 0.10, "cash": 0.05},
    "aggressive":   {"stocks": 0.80, "bonds": 0.10, "gold": 0.05, "cash": 0.05},
}
# Glide path: maximum equity share allowed for a given horizon (years).
# Short horizons cannot recover from a crash, whatever the stated risk appetite.
HORIZON_EQUITY_CAPS = [(3, 0.20), (5, 0.40), (8, 0.60)]   # (horizon < N years, max equity)
# Age ceiling: max equity = (AGE_RULE_BASE - age) / 100, clipped to [0.2, 0.9]
AGE_RULE_BASE = 110
# Blended-volatility ceiling per risk profile — the Validator Agent enforces this.
RISK_VOL_CEILING = {"conservative": 0.09, "moderate": 0.15, "aggressive": 0.22}

MAX_SINGLE_STOCK_WEIGHT = 0.35   # within the equity sleeve
SECTOR_CAP = 0.40                # max share of the equity sleeve in one sector
MAX_PER_SECTOR_IN_SHORTLIST = 2
NUM_STOCKS_TO_SELECT = 6
MIN_EFFECTIVE_HOLDINGS = 3.0     # 1/sum(w^2) — guards against concentration
MAX_OPTIMIZATION_ATTEMPTS = 3    # optimizer <-> validator loop limit
# Below this equity budget, buying individual shares isn't practical (one share
# can exceed the whole budget) -> the stock portion goes to a Nifty 50 index fund.
MIN_DIRECT_EQUITY_RUPEES = 5000

# --------------------------------------------------------------------------
# Simulation & backtest
# --------------------------------------------------------------------------
MONTE_CARLO_SIMULATIONS = 5000
STUDENT_T_DF = 5                  # fat tails: crashes are more common than a normal curve implies
BACKTEST_TEST_DAYS = 252          # last ~1 year is held out for out-of-sample testing
