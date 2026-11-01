"""
News & Sentiment Agent
-------------------------
Reads recent headlines for each stock in the universe and answers two
questions:

  1. What is the general mood? A sentiment score from -1 (very negative)
     to +1 (very positive), shown to the user for context only. It does NOT
     change the weights: headline sentiment is a weak, noisy signal, and we
     don't want a portfolio that flips with the news cycle.
  2. Is there a RED FLAG? A "severe" flag means a headline about fraud, a
     regulator probe or ban, default/insolvency, raids, arrests, accounting
     problems, etc. The Validator Agent refuses portfolios that hold a flagged
     stock and tells the optimizer to drop it (rule 9).

Sources (both free, no key):
  * yfinance `Ticker.news` (two different item shapes exist across versions —
    both are handled)
  * Google News RSS search as a fallback when yfinance returns nothing
Results are cached in SQLite for NEWS_CACHE_TTL_HOURS (6 h).

Classification:
  * With an LLM configured: ONE batched JSON call for all stocks. The LLM may
    only flag a stock as severe by pointing at one of the headlines it was
    given; a flag citing a headline that doesn't exist is ignored.
  * Without an LLM (or if the call fails): a small finance word list for the
    score plus SEVERE_PATTERNS regexes for red flags.
In offline mode (tests / demos) it returns status "no_data" and flags nothing:
synthetic prices have no real news, and inventing some would be dishonest.
"""
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import database
import llm
from config import OFFLINE_MODE

log = logging.getLogger("whitefin.news")

NEWS_CACHE_TTL_HOURS = 6
MAX_HEADLINES = 6
FETCH_TIMEOUT = 8

SEVERE_PATTERNS = [
    r"\bfraud\w*", r"\bscam\b", r"\bponzi\b", r"\bembezzl\w*", r"\bmoney[- ]laundering\b",
    r"\bsebi\b.{0,40}\b(probe|ban|bars|order|penalt\w*|notice|investigat\w*)",
    r"\b(ed|cbi|income[- ]tax)\b.{0,20}\b(raid\w*|search\w*|probe)", r"\braid(s|ed)?\b",
    r"\binsolven\w*", r"\bbankrupt\w*", r"\bdefault(s|ed)? on\b", r"\bnclt\b",
    r"\baccounting (irregularit\w*|fraud|lapse\w*)", r"\bauditor resign\w*", r"\bforensic audit\b",
    r"\barrest(ed|s)?\b", r"\bdelist\w*", r"\bwhistle-?blower\b",
]
_SEVERE_RE = [re.compile(p, re.I) for p in SEVERE_PATTERNS]

POSITIVE = {
    "beat", "beats", "surge", "surges", "jump", "jumps", "rally", "rallies", "record", "gain", "gains",
    "rise", "rises", "upgrade", "upgrades", "upgraded", "growth", "profit", "profits", "strong", "outperform",
    "buy", "bullish", "expands", "expansion", "wins", "win", "order", "orders", "dividend", "high", "soars",
}
NEGATIVE = {
    "miss", "misses", "fall", "falls", "drop", "drops", "plunge", "plunges", "slump", "slumps", "loss",
    "losses", "downgrade", "downgrades", "downgraded", "weak", "cut", "cuts", "decline", "declines", "sell",
    "bearish", "probe", "penalty", "fine", "fined", "lawsuit", "concern", "concerns", "low", "crash", "warning",
}


# ------------------------------------------------------------------ fetching
def _iso(ts) -> str | None:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(ts, str) and ts:
            return ts[:10]
    except Exception:
        pass
    return None


def _parse_yf_item(item: dict) -> dict | None:
    """yfinance < 0.2.50: flat dict. Newer: {"id":..., "content": {...}}."""
    c = item.get("content") if isinstance(item.get("content"), dict) else None
    if c:
        title = c.get("title")
        publisher = (c.get("provider") or {}).get("displayName")
        link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
        published = _iso(c.get("pubDate") or c.get("displayTime"))
    else:
        title = item.get("title")
        publisher = item.get("publisher")
        link = item.get("link")
        published = _iso(item.get("providerPublishTime"))
    if not title:
        return None
    return {"title": title.strip(), "publisher": publisher, "link": link, "published": published}


def _from_yfinance(ticker: str) -> list[dict]:
    import yfinance as yf
    items = yf.Ticker(ticker).news or []
    out = [h for h in (_parse_yf_item(i) for i in items) if h]
    return out[:MAX_HEADLINES]


def _from_google_rss(query: str) -> list[dict]:
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} share") +
           "&hl=en-IN&gl=IN&ceid=IN:en")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 WhiteFIN"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        published = None
        try:
            published = parsedate_to_datetime(it.findtext("pubDate")).strftime("%Y-%m-%d")
        except Exception:
            pass
        out.append({"title": title, "publisher": it.findtext("source"), "link": it.findtext("link"),
                    "published": published})
        if len(out) == MAX_HEADLINES:
            break
    return out


def fetch_headlines(ticker: str, name: str | None) -> tuple[list[dict], str]:
    key = f"news:{ticker}"
    cached = database.cache_get(key, ttl_hours=NEWS_CACHE_TTL_HOURS)
    if cached is not None:
        return cached["headlines"], cached["source"]
    headlines, source = [], "none"
    try:
        headlines = _from_yfinance(ticker)
        source = "yfinance" if headlines else "none"
    except Exception as exc:
        log.info("yfinance news failed for %s: %s", ticker, exc)
    if not headlines:
        try:
            headlines = _from_google_rss(name or ticker.replace(".NS", ""))
            source = "google_news_rss" if headlines else "none"
        except Exception as exc:
            log.info("Google News RSS failed for %s: %s", ticker, exc)
    if headlines:
        database.cache_set(key, {"headlines": headlines, "source": source})
    return headlines, source


# ------------------------------------------------------------------ classification
def severe_match(text: str) -> str | None:
    for rx in _SEVERE_RE:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def lexicon_score(titles: list[str]) -> float:
    pos = neg = 0
    for t in titles:
        words = re.findall(r"[a-z]+", t.lower())
        pos += sum(w in POSITIVE for w in words)
        neg += sum(w in NEGATIVE for w in words)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 2)


def classify_lexicon(per_ticker: dict) -> dict:
    out = {}
    for t, heads in per_ticker.items():
        titles = [h["title"] for h in heads]
        severe_hits = [(h["title"], severe_match(h["title"])) for h in heads if severe_match(h["title"])]
        out[t] = {
            "sentiment": lexicon_score(titles),
            "severe": bool(severe_hits),
            "severe_reason": (f"Headline mentions '{severe_hits[0][1]}': \"{severe_hits[0][0]}\""
                              if severe_hits else None),
        }
    return out


def classify_llm(per_ticker: dict, names: dict) -> dict | None:
    if not llm.provider():
        return None
    lines = []
    for t, heads in per_ticker.items():
        if not heads:
            continue
        lines.append(f"{t} ({names.get(t) or t}):")
        lines += [f"  [{i}] {h['title']}" for i, h in enumerate(heads)]
    if not lines:
        return None
    prompt = (
        "You are a cautious equity analyst. For each Indian stock below, read its recent headlines and return "
        "JSON of the form {\"TICKER\": {\"sentiment\": number from -1 to 1, \"severe\": true/false, "
        "\"severe_headline\": index or null, \"reason\": short text}}. "
        "Set severe=true ONLY for serious red flags about THAT company: fraud, regulator (SEBI/ED/CBI) action, "
        "raids, arrests of leadership, default, insolvency, accounting irregularities. Ordinary bad results, "
        "price falls or downgrades are NOT severe. Include every ticker listed.\n\n" + "\n".join(lines)
    )
    data = llm.complete_json(prompt)
    if not isinstance(data, dict):
        return None
    out = {}
    for t, heads in per_ticker.items():
        d = data.get(t) or {}
        try:
            s = float(d.get("sentiment", 0))
        except (TypeError, ValueError):
            s = 0.0
        severe, reason = False, None
        idx = d.get("severe_headline")
        # A red flag must point at a real headline we gave it; otherwise ignore it.
        if d.get("severe") is True and isinstance(idx, int) and 0 <= idx < len(heads):
            severe = True
            reason = f"{d.get('reason') or 'Serious red flag'}: \"{heads[idx]['title']}\""
        # The deterministic patterns still apply, so an LLM can't hide a fraud headline.
        if not severe:
            for h in heads:
                hit = severe_match(h["title"])
                if hit:
                    severe, reason = True, f"Headline mentions '{hit}': \"{h['title']}\""
                    break
        out[t] = {"sentiment": round(max(-1.0, min(1.0, s)), 2), "severe": severe, "severe_reason": reason}
    return out


def _label(score: float) -> str:
    return "positive" if score >= 0.25 else "negative" if score <= -0.25 else "neutral"


# ------------------------------------------------------------------ agent entry point
def run(market_data: dict) -> dict:
    tickers = list(market_data["stocks"])
    names = {t: market_data["stocks"][t].get("name") for t in tickers}

    if OFFLINE_MODE or market_data.get("data_source") == "offline_fallback":
        return {
            "status": "no_data", "method": None, "flagged": [],
            "note": "Offline/demo mode — no live news, so nothing is flagged.",
            "per_ticker": {t: {"sentiment": None, "label": "no data", "headline_count": 0, "headlines": [],
                               "severe": False, "severe_reason": None, "source": "none"} for t in tickers},
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = dict(zip(tickers, pool.map(lambda t: fetch_headlines(t, names[t]), tickers)))
    per_ticker_heads = {t: fetched[t][0] for t in tickers}

    classified = classify_llm(per_ticker_heads, names)
    method = f"llm ({llm.provider()})" if classified else "lexicon"
    if not classified:
        classified = classify_lexicon(per_ticker_heads)

    per_ticker = {}
    for t in tickers:
        heads = per_ticker_heads[t]
        c = classified.get(t, {"sentiment": 0.0, "severe": False, "severe_reason": None})
        has = bool(heads)
        per_ticker[t] = {
            "sentiment": c["sentiment"] if has else None,
            "label": _label(c["sentiment"]) if has else "no data",
            "headline_count": len(heads),
            "headlines": heads[:3],
            "severe": bool(c["severe"]) and has,
            "severe_reason": c["severe_reason"] if has else None,
            "source": fetched[t][1],
        }
    with_news = sum(1 for v in per_ticker.values() if v["headline_count"])
    status = "ok" if with_news == len(tickers) else "partial" if with_news else "no_data"
    return {
        "status": status,
        "method": method,
        "flagged": sorted(t for t, v in per_ticker.items() if v["severe"]),
        "note": "Sentiment is shown for context only; it never changes the weights. "
                "Only severe red flags affect the portfolio (the Validator drops those stocks).",
        "per_ticker": per_ticker,
    }
