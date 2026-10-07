"""
Market Intelligence Agent
---------------------------
Fetches prices, fundamentals, a gold proxy and the Nifty 50 benchmark.

What changed vs. v1 (and why):
  * ONE batched `yf.download()` call for all prices instead of 16 sequential
    `.history()` calls, plus parallel `.info` calls -> much faster.
  * Results cached in SQLite for MARKET_CACHE_TTL_HOURS, so repeated analyses
    don't re-download (Yahoo rate-limits aggressively).
  * Price history and fundamentals are fetched separately: if `.info` fails,
    the real price history is still used.
  * yfinance reports debtToEquity as a PERCENTAGE (35.5 == 0.355). It is now
    divided by 100 so live and fallback numbers share one unit.
  * Every date index is made timezone-naive. Mixing tz-aware (live) and naive
    (synthetic) indexes made `dropna()` wipe out every row in v1.
  * The synthetic fallback is seeded with zlib.crc32 (stable across runs) —
    Python's built-in hash() is randomised per process. Synthetic stocks now
    share a common "market factor", so they correlate like real stocks do.
  * `data_source` is "live" / "mixed" / "offline_fallback" plus an exact
    `live_ratio`, instead of "live" whenever 1 of 15 tickers loaded.
"""
import logging
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from config import (
    STOCK_UNIVERSE, GOLD_TICKER, BENCHMARK_TICKER, HISTORY_PERIOD,
    MIN_HISTORY_POINTS, OFFLINE_MODE,
)
import database

log = logging.getLogger("whitefin.market")

FINANCIAL_SECTORS = {"Financial Services", "Banking", "Financials"}

# Illustrative figures, used ONLY when live data is unavailable. Sector names
# follow yfinance's naming so live and fallback data group the same way.
_FALLBACK_INFO = {
    "RELIANCE.NS":   dict(name="Reliance Industries", price=2900, pe=24, pb=2.3, roe=0.10, de=0.35, margin=0.08, dy=0.004, beta=1.00, sector="Energy"),
    "TCS.NS":        dict(name="Tata Consultancy Services", price=3800, pe=27, pb=13.0, roe=0.45, de=0.05, margin=0.19, dy=0.015, beta=0.75, sector="Technology"),
    "INFY.NS":       dict(name="Infosys", price=1650, pe=25, pb=8.0, roe=0.30, de=0.08, margin=0.17, dy=0.025, beta=0.80, sector="Technology"),
    "HDFCBANK.NS":   dict(name="HDFC Bank", price=1650, pe=19, pb=2.7, roe=0.16, de=None, margin=0.22, dy=0.012, beta=0.95, sector="Financial Services"),
    "ICICIBANK.NS":  dict(name="ICICI Bank", price=1200, pe=18, pb=3.1, roe=0.17, de=None, margin=0.24, dy=0.008, beta=1.05, sector="Financial Services"),
    "HINDUNILVR.NS": dict(name="Hindustan Unilever", price=2500, pe=52, pb=10.0, roe=0.20, de=0.05, margin=0.16, dy=0.017, beta=0.55, sector="Consumer Defensive"),
    "ITC.NS":        dict(name="ITC", price=430, pe=24, pb=6.5, roe=0.28, de=0.02, margin=0.27, dy=0.031, beta=0.60, sector="Consumer Defensive"),
    "KOTAKBANK.NS":  dict(name="Kotak Mahindra Bank", price=1750, pe=18, pb=2.6, roe=0.14, de=None, margin=0.25, dy=0.001, beta=0.90, sector="Financial Services"),
    "LT.NS":         dict(name="Larsen & Toubro", price=3600, pe=32, pb=4.8, roe=0.15, de=0.40, margin=0.07, dy=0.008, beta=1.10, sector="Industrials"),
    "SBIN.NS":       dict(name="State Bank of India", price=820, pe=10, pb=1.6, roe=0.17, de=None, margin=0.15, dy=0.017, beta=1.25, sector="Financial Services"),
    "BHARTIARTL.NS": dict(name="Bharti Airtel", price=1500, pe=55, pb=9.5, roe=0.17, de=0.90, margin=0.10, dy=0.005, beta=0.85, sector="Communication Services"),
    "ASIANPAINT.NS": dict(name="Asian Paints", price=2900, pe=48, pb=13.0, roe=0.26, de=0.06, margin=0.15, dy=0.011, beta=0.70, sector="Basic Materials"),
    "MARUTI.NS":     dict(name="Maruti Suzuki", price=12500, pe=27, pb=4.2, roe=0.16, de=0.02, margin=0.09, dy=0.010, beta=0.90, sector="Consumer Cyclical"),
    "TITAN.NS":      dict(name="Titan Company", price=3400, pe=68, pb=22.0, roe=0.32, de=0.30, margin=0.07, dy=0.003, beta=1.00, sector="Consumer Cyclical"),
    "SUNPHARMA.NS":  dict(name="Sun Pharmaceutical", price=1750, pe=36, pb=6.5, roe=0.18, de=0.05, margin=0.20, dy=0.008, beta=0.65, sector="Healthcare"),
}
_GENERIC_FALLBACK = dict(name=None, price=1000, pe=None, pb=None, roe=None, de=None, margin=None, dy=None, beta=1.0, sector="Unknown")
_GOLD_FALLBACK_PRICE = 68.0
_SYNTH_DAYS = 1250  # ~5 trading years


# ------------------------------------------------------------------ helpers
def _seed(name: str) -> int:
    return zlib.crc32(name.encode())  # stable across processes, unlike hash()


def _naive_dates(index) -> pd.DatetimeIndex:
    idx = pd.to_datetime(index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def _clean_series(s: pd.Series) -> pd.Series:
    s = s.dropna().astype(float)
    s.index = _naive_dates(s.index)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _synthetic_market(days: int = _SYNTH_DAYS) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """A fat-tailed 'market factor' (daily returns) shared by every synthetic stock."""
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    rng = np.random.default_rng(_seed("MARKET"))
    t = rng.standard_t(df=5, size=days) / np.sqrt(5 / 3)  # unit variance
    mkt = 0.11 / 252 + (0.15 / np.sqrt(252)) * t
    return idx, mkt


def _synthetic_series(name: str, start_price: float, beta: float, idio_vol: float,
                      drift: float, idx, mkt) -> pd.Series:
    rng = np.random.default_rng(_seed(name))
    idio = rng.normal(0, idio_vol / np.sqrt(252), size=len(idx))
    r = drift / 252 + beta * (mkt - 0.11 / 252) + idio
    prices = np.cumprod(1 + r)
    prices = start_price * prices / prices[-1]  # end at the reference price
    return pd.Series(prices, index=idx)


def _num(x):
    try:
        if x is None:
            return None
        x = float(x)
        return None if np.isnan(x) else x
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ live fetch
def _download_prices(tickers: list[str]) -> dict[str, pd.Series]:
    key = f"prices:{HISTORY_PERIOD}:{zlib.crc32(','.join(sorted(tickers)).encode())}"
    cached = database.cache_get(key)
    if cached:
        return {t: _clean_series(pd.Series(v)) for t, v in cached.items()}, True

    import yfinance as yf
    df = yf.download(tickers, period=HISTORY_PERIOD, auto_adjust=True,
                     progress=False, threads=True, timeout=15)
    if df is None or df.empty:
        return {}, False
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]
    else:  # single ticker, flat columns
        close = df[["Close"]].rename(columns={"Close": tickers[0]})

    out = {}
    for t in close.columns:
        s = _clean_series(close[t])
        if len(s) >= MIN_HISTORY_POINTS:
            out[str(t)] = s
    if out:
        database.cache_set(key, {t: {d.strftime("%Y-%m-%d"): float(v) for d, v in s.items()}
                                 for t, s in out.items()})
    return out, False


def _fetch_info(ticker: str) -> dict | None:
    cached = database.cache_get(f"info:{ticker}", ttl_hours=24)
    if cached:
        return cached
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        if not info or len(info) < 5:
            return None
        slim = {k: info.get(k) for k in (
            "trailingPE", "priceToBook", "returnOnEquity", "debtToEquity",
            "profitMargins", "dividendYield", "sector", "shortName", "longName")}
        database.cache_set(f"info:{ticker}", slim)
        return slim
    except Exception as exc:
        log.info("info fetch failed for %s: %s", ticker, exc)
        return None


# ------------------------------------------------------------------ assemble
def _stock_record(ticker: str, history: pd.Series | None, info: dict | None, idx, mkt) -> dict:
    fb = _FALLBACK_INFO.get(ticker, _GENERIC_FALLBACK)
    if history is not None:
        source = "live"
    else:
        history = _synthetic_series(ticker, fb["price"], fb["beta"], 0.20, 0.10 + 0.04 * (fb["beta"] - 1), idx, mkt)
        source = "offline_fallback"

    if info:
        de = _num(info.get("debtToEquity"))
        dy = _num(info.get("dividendYield"))
        record = dict(
            name=info.get("shortName") or info.get("longName") or fb["name"] or ticker.replace(".NS", ""),
            sector=info.get("sector") or fb["sector"],
            pe=_num(info.get("trailingPE")),
            pb=_num(info.get("priceToBook")),
            roe=_num(info.get("returnOnEquity")),
            de=de / 100 if de is not None else None,            # % -> ratio
            profit_margin=_num(info.get("profitMargins")),
            # yfinance has returned dividendYield both as a fraction and as a %
            dividend_yield=(dy / 100 if dy is not None and dy > 1 else dy),
            info_source="live",
        )
    else:
        record = dict(
            name=fb["name"] or ticker.replace(".NS", ""), sector=fb["sector"],
            pe=fb["pe"], pb=fb["pb"], roe=fb["roe"], de=fb["de"],
            profit_margin=fb["margin"], dividend_yield=fb["dy"], info_source="fallback",
        )

    record.update(
        ticker=ticker,
        price=float(history.iloc[-1]),
        history=history,
        source=source,
        is_financial=record["sector"] in FINANCIAL_SECTORS,
    )
    return record


def run() -> dict:
    tickers = list(dict.fromkeys(STOCK_UNIVERSE))
    notes: list[str] = []
    prices: dict[str, pd.Series] = {}
    infos: dict[str, dict | None] = {}
    from_cache = False

    if OFFLINE_MODE:
        notes.append("Offline mode is on — all market data is synthetic demo data.")
    else:
        try:
            prices, from_cache = _download_prices(tickers + [GOLD_TICKER, BENCHMARK_TICKER])
        except Exception as exc:
            log.warning("price download failed: %s", exc)
            notes.append(f"Live price download failed ({exc.__class__.__name__}).")
        if prices:
            live_tickers = [t for t in tickers if t in prices]
            with ThreadPoolExecutor(max_workers=8) as pool:
                for t, info in zip(live_tickers, pool.map(_fetch_info, live_tickers)):
                    infos[t] = info
        else:
            notes.append("No live data reachable — using synthetic demo data.")

    idx, mkt = _synthetic_market()
    stocks = {t: _stock_record(t, prices.get(t), infos.get(t), idx, mkt) for t in tickers}

    live = [t for t, s in stocks.items() if s["source"] == "live"]
    live_ratio = len(live) / max(1, len(tickers))
    missing_info = [t for t in live if stocks[t]["info_source"] != "live"]
    if live and len(live) < len(tickers):
        notes.append(f"{len(tickers) - len(live)} of {len(tickers)} tickers fell back to synthetic data.")
    if missing_info:
        notes.append(f"Fundamentals unavailable for {len(missing_info)} live tickers — illustrative figures used.")

    # Gold proxy
    if GOLD_TICKER in prices:
        gold_hist, gold_src = prices[GOLD_TICKER], "live"
    else:
        gold_hist = _synthetic_series(GOLD_TICKER, _GOLD_FALLBACK_PRICE, -0.05, 0.13, 0.08, idx, mkt)
        gold_src = "offline_fallback"
    gold = {"ticker": GOLD_TICKER, "name": "Nippon India Gold ETF (GOLDBEES)", "sector": "Gold ETF",
            "price": float(gold_hist.iloc[-1]), "history": gold_hist, "source": gold_src}

    # Benchmark: real Nifty 50 if possible, otherwise the best proxy available
    if BENCHMARK_TICKER in prices:
        bench = {"name": "Nifty 50", "history": prices[BENCHMARK_TICKER], "source": "live"}
    elif len(live) >= 3:
        rets = pd.DataFrame({t: stocks[t]["history"] for t in live}).pct_change().mean(axis=1).fillna(0)
        bench = {"name": "Equal-weight universe (Nifty unavailable)", "history": 100 * (1 + rets).cumprod(),
                 "source": "proxy"}
    else:
        bench = {"name": "Synthetic market index (demo)", "history": pd.Series(100 * np.cumprod(1 + mkt), index=idx),
                 "source": "offline_fallback"}

    data_source = "live" if live_ratio == 1 else ("mixed" if live else "offline_fallback")
    return {
        "stocks": stocks,
        "gold": gold,
        "benchmark": bench,
        "live_ratio": round(live_ratio, 3),
        "data_source": data_source,
        "from_cache": from_cache,
        "notes": notes,
    }
