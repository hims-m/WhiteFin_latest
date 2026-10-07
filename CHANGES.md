# WhiteFIN v1 → v2: what changed and why

Each item below was a problem found in the v1 code review. For each one: the problem, the fix, and where the fix lives.

## Bugs that produced wrong output

| # | v1 problem | v2 fix | Where |
|---|---|---|---|
| 1 | "I'm 20" wasn't recognised as an age. The README's own example silently used age 30. | Every number is classified by its context. "I'm 20", "aged 19" and "20 years old" are ages. "For 10 years" and "in 18 months" are horizons. "₹5,000 per month" is a SIP. "1.5 lakh", "50k" and "2 crore" are amounts. | `user_profiling.py` |
| 2 | "I don't want high risk" was read as **aggressive**. | Negation detection, with exceptions such as "don't mind" and "not too risky". When the text names two levels, the more cautious one is used. | `user_profiling.py` |
| 3 | Missing values were silently defaulted (₹50,000, age 30, moderate). | The graph routes to a **clarification** node and the UI asks. "Just use defaults" is possible, and every assumption is listed. | `user_profiling.py`, `workflow.py` |
| 4 | `hash(ticker)` changes on every run, so the "seeded" synthetic data was different after every restart. | Seeds now come from `zlib.crc32`. Synthetic stocks also share a market factor, so they correlate realistically. | `market_intelligence.py` |
| 5 | yfinance `debtToEquity` is a percentage (35.5), but the fallback data used a ratio (0.35). | Live values are divided by 100. Banks don't use D/E at all, because deposits are liabilities. | `market_intelligence.py`, `fundamental_analysis.py` |
| 6 | Synthetic random walks were optimized as if they were real. The data was labelled "live" if even 1 of 15 tickers loaded. Tz-aware and tz-naive dates made `dropna()` wipe out all rows. | Data is labelled `live`, `mixed` or `offline_fallback` with an exact `live_ratio`. Synthetic tickers are excluded when at least 3 live tickers exist. All dates are tz-naive, and the UI shows a banner for non-live data. | `market_intelligence.py`, `portfolio_optimization.py` |
| 7 | The `gemini-1.5-flash` model is retired, and `except: return None` hid the failure. | The model is configurable. Failures are logged. Ollama works as a local alternative. | `llm.py` |
| 8 | Max-Sharpe used PyPortfolioOpt's default 2% risk-free rate. | Uses 6.5% (Indian 10-year G-Sec), configurable. | `config.py`, `portfolio_optimization.py` |
| 9 | Risk metrics were computed but never used in any decision. `portfolio_risk()` was dead code. | Betas feed expected returns. Portfolio VaR, CVaR and drawdown are computed for the sleeve. The Validator enforces volatility limits. | `risk_assessment.py`, `validator.py` |
| 10 | Fewer than 3 stocks × a 35% cap can never reach 100%, so the optimizer silently fell back to equal weights. | Feasibility (stock cap and sector cap) is checked before solving; the cap is relaxed and the reason reported. A fresh `EfficientFrontier` is used for every attempt. | `portfolio_optimization.py` |

## Methodology

| v1 | v2 | Why |
|---|---|---|
| `mean_historical_return` over 2 years | Expected return = rf + β × (E[Rm] − rf) + fundamental tilt, using 5 years of data for β | Historical means are very noisy. A bull run produced 30%+ "expected" returns that the Monte Carlo then compounded for decades. |
| Blended volatility = weighted sum of volatilities | √(wᵀΣw) with the measured equity–gold correlation | A linear sum assumes correlation = 1, which hides the reason to hold gold. The UI shows the diversification benefit. |
| Age and horizon ignored | Glide path by horizon plus an age rule cap equity | Money needed in 2 years can't wait out a crash. |
| Min-max scaling across the universe | Percentile ranks, sector-relative when the sector has ≥3 stocks; bank-specific weights; loss-makers get the worst P/E score | One outlier no longer squashes the scale. Banks are judged on P/B and ROE. |
| No sector limit | Max 2 per sector in the shortlist, plus a 40% sector cap in the optimizer | Prevents a portfolio that is three banks. |
| Fixed 18% target volatility for moderate | Midpoint between the min-volatility and max-Sharpe portfolios | Adapts to the actual frontier. |
| Fractional weights only | Whole shares (`DiscreteAllocation`), ₹ amounts, leftover cash; index fund below ₹5,000 | ₹50,000 can't buy 0.2 of a Maruti share. |
| Yearly normal returns, lump sum only | Monthly Student-t (fat tails), SIP contributions, inflation-adjusted outcomes, P(loss) vs total invested | Closer to how people invest and how markets crash. |
| No backtest | Out-of-sample: train on older data, test on the last unseen year vs Nifty 50 and equal weight | Answers "is it better than an index fund?" honestly. |
| "Confidence score" | "Data quality score" | It measures data availability, not recommendation quality. |
| The LLM was told not to change numbers | Every number in the LLM's text is verified; the rewrite is rejected if any differ | Trust, but verify. |

## Making it agentic

| v1 | v2 |
|---|---|
| A fixed linear chain of 7 nodes | Conditional routing: the clarification gate |
| No feedback | Validator → optimizer loop with concrete, accumulating feedback: exclude stock, max share price, number of stocks, caps, equity cut |
| Sequential only | Two parallel fan-outs, each with a join |
| Invisible | Every node appends to `trace`, streamed live over Server-Sent Events |

## Engineering

- **Speed and caching:** one batched `yf.download`, parallel `.info` calls, and a SQLite cache with a time limit.
- **Typed API:** Pydantic models with range checks, `Literal` risk levels, nested response models, and a capped `limit` on history.
- **Server fixes:**
  - CORS uses an explicit origin list, with credentials off.
  - The `lifespan` handler replaces the deprecated `on_event`.
  - Errors return a generic message; details go to the log.
- **Packaging:** the frontend is served by FastAPI (one command), and Docker plus docker-compose are included.
- **Quality:** 52 offline pytest tests, including a mocked live-data path and fake-LLM tests, run in CI on Python 3.11–3.13.
- **Configuration:** every assumption lives in `config.py` / `.env`.
- **Dates:** `datetime.utcnow` was replaced with a timezone-aware equivalent.

## v2.1: explanations for beginners and experienced investors

- **Experience level.** The profiling agent sets `experience` to `beginner` or `advanced`.
  - Beginner cues: "new to investing", "first time", "explain simply".
  - Advanced: two or more investing terms in the prompt.
  - It can also be set from the form or the API, and defaults to beginner. It never affects the portfolio.
- **Structured explanation.** Sections now have a lead line and bullet points instead of a paragraph. "Stocks chosen" became one card per stock: weight, what to buy, ✓ strengths, ! one thing to watch.
  - Beginner cards use plain phrases ("Carries very little debt").
  - Experienced cards show the metric, its sector percentile, E[R], β and risk share.
- **Two versions every time.** `explanation.levels` and `explanation.overview` contain both, so the UI's **View as** toggle switches without another request.
- **"In a nutshell."** A 2–3 sentence overview per level. When an LLM is configured, it rewrites only this overview, in the reader's style, and the rewrite is still number-verified. This is also much faster for small local models than rewriting the whole report.
- **New analytics for experienced readers:**
  - efficient frontier under the same caps, with the chosen sleeve marked;
  - each holding's share of capital vs share of risk, w_i(Σw)_i / wᵀΣw;
  - a daily-return correlation heatmap with the average pairwise ρ.
- **Parser fix.** "10y" is now read as a 10-year horizon.
- **Tests.** 60 tests: experience detection and override, both levels present, no jargon in the beginner text, and analytics validity.
