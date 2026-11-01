---
title: WhiteFIN
emoji: 📈
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Agentic multi-agent portfolio assistant (educational)
---

# WhiteFIN v2 — Agentic AI Investment Assistant

WhiteFIN turns a plain-English request such as *"I have ₹1,00,000 and can add ₹5,000 a month. I'm 22, moderate risk, investing for 15 years"* into a researched, validated, explained portfolio. It is built entirely on free, open-source tools, and it runs with no API keys and no signup.

Eleven agents cooperate through a **LangGraph state machine that makes decisions at runtime**. It doesn't just call functions in a fixed order:

- **It asks instead of guessing.** If your message doesn't say how much, how long or how much risk, the profiling agent routes to a clarification step and the UI asks you.
- **A critic revises the work.** The Validator Agent checks the portfolio against 9 rules, including "no stock with a severe news red flag". When a rule fails, it sends the portfolio back to the optimizer with concrete fixes, such as "drop MARUTI, cap share price at ₹2,667, hold 4 stocks." This loops until the portfolio passes or 3 attempts are used.
- **It picks its method from evidence.** The Strategy Selector builds the equity sleeve two ways, mean-variance (MPT) and Hierarchical Risk Parity (HRP), using only older data. It tests both on the hidden last year and keeps the better risk-adjusted one. The Validator can overrule it and switch to HRP when MPT keeps concentrating the portfolio.
- **It reads the news.** The News & Sentiment Agent pulls recent headlines per stock and scores the mood (shown for context only). If it finds a red flag such as fraud, a SEBI probe or insolvency, the Validator drops that stock.
- **Independent agents run in parallel.** Fundamentals ∥ Risk ∥ News, then Monte Carlo ∥ Backtest, with joins that wait for every branch.
- **You can watch it think.** Every agent's step streams live to the dashboard.

**Explanations adapt to the reader.** The profiling agent detects whether you write like a beginner ("I'm new to investing") or an experienced investor (two or more terms such as Sharpe, drawdown, beta or midcap). You can also pick the level yourself.

- **Beginner view:** plain words, short bullet points, "four jars" for the asset mix, and one card per stock saying what to buy and why.
- **Experienced view:** standard terms (E[R], σ, Sharpe, VaR/CVaR, β, CAPM), plus an efficient-frontier chart, capital-vs-risk contribution, a correlation heatmap, and the full validator and universe tables.

Both versions are generated on every run, so the **View as** toggle switches instantly without re-running the agents. The level only changes the explanation, never the portfolio.

> Educational project, not investment advice. Consult a SEBI-registered investment adviser before investing real money.

---

## 1. Architecture

```
START → User Profiling ──(missing info?)──► Clarify ──► END   (UI asks, then resubmits)
              │ no
              ▼
      Market Intelligence ── one batched download, SQLite cache, tz-safe, live/mixed/offline
              │
     ┌────────────────┼─────────────────┐  parallel
     ▼                ▼                 ▼
Fundamental     Risk Assessment     News & Sentiment
 Analysis       (vol, VaR, CVaR,    (yfinance / Google News RSS,
                 drawdown, beta)     LLM or word-list, red flags)
     └────────────────┼─────────────────┘  join
                      ▼
            Strategy Selector ── MPT vs HRP bake-off on the held-out year
                      ▼
   ┌──► Portfolio Optimization ── glide path · CAPM returns + fundamental tilt
   │          │                    · sector caps · whole-share execution
   │          ▼
   └─── Validator ── 9 checks; on failure writes feedback for the next attempt
  revise      │      (drop stock, tighten caps, cut equity, switch to HRP)
              │ approved
     ┌────────┴────────┐  parallel
     ▼                 ▼
Scenario Simulation   Backtest
(fat-tailed, SIP,     (out-of-sample vs Nifty 50)
 inflation)
     └────────┬────────┘  join
              ▼
       Explainability ── template text; optional LLM rewrite is number-verified
              ▼
             END
```

`GET /api/graph` returns the live graph as a Mermaid diagram.

| Agent | File | What it decides |
|---|---|---|
| User Profiling | `agents/user_profiling.py` | Amount, SIP, age, risk, horizon, goal; what's missing; negation ("don't want high risk") |
| Market Intelligence | `agents/market_intelligence.py` | Prices, fundamentals, gold, Nifty 50; whether data is live, mixed or synthetic |
| Fundamental Analysis | `agents/fundamental_analysis.py` | Sector-relative percentile scores; bank-specific scoring |
| Risk Assessment | `agents/risk_assessment.py` | Per-stock and portfolio risk metrics |
| News & Sentiment | `agents/news_sentiment.py` | Headline mood per stock (informational); severe red flags (fraud, regulator action, default) |
| Strategy Selector | `agents/strategy_selector.py` | MPT or HRP for this request, from an out-of-sample bake-off |
| Portfolio Optimization | `agents/portfolio_optimization.py` | Asset mix, stock shortlist, weights (MPT or HRP), share quantities |
| Validator | `agents/validator.py` | Approve, or revise with specific feedback (incl. dropping red-flagged stocks, switching to HRP) |
| Scenario Simulation | `agents/scenario_simulation.py` | Range of outcomes in nominal and today's rupees |
| Backtest | `agents/backtest.py` | Would these weights have beaten Nifty out-of-sample? |
| Explainability | `agents/explainability.py` | Plain-language "why"; rejects LLM text that changes numbers |

---

## 2. Quickstart (one command)

Requires **Python 3.11+** (tested on 3.13).

```bash
cd whitefin/backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open **http://127.0.0.1:8000**. The backend serves the dashboard too.

- Interactive API docs are at http://127.0.0.1:8000/docs.
- No internet, or Yahoo Finance blocked? The app switches to synthetic demo data and says so in a banner. To force this mode, set `WHITEFIN_OFFLINE=1`.
- Prefer a separate static server? Run `python -m http.server 5500` inside `frontend/`. The page then talks to `:8000` automatically.

### Docker

```bash
docker compose up --build        # → http://localhost:8000
```

### Deploy free on Hugging Face Spaces

The repo is already set up for a **Docker Space**: the front matter at the top of this README says `sdk: docker` and `app_port: 7860`, and the Dockerfile runs as user 1000 on port 7860.

1. On huggingface.co: **New Space** → name `whitefin` → SDK **Docker** → template **Blank** → hardware **CPU basic (free)**.
2. In the Space's **Settings → Variables and secrets** (optional): add the secret `GEMINI_API_KEY` and the variable `GEMINI_MODEL`. Ollama can't run on the free tier, so Gemini is the LLM option there.
3. Create a **write** access token (Settings → Access Tokens).
4. Push this repo to the Space (use the token as the password):

   ```bash
   git remote add hf https://huggingface.co/spaces/<your-username>/whitefin
   git push hf main
   ```

The Space builds the image (a few minutes) and goes live at `https://<your-username>-whitefin.hf.space`. Things to know:

- **Storage isn't permanent.** History is in `/tmp` and resets when the Space restarts or sleeps after 48 hours without visitors.
- **Yahoo Finance may refuse cloud servers.** If it does, the app falls back to demo data and shows the banner.

### Optional: add an LLM (still free)

The app is complete without an LLM. With one, the profiling agent understands vaguer messages and the explanation reads more naturally. Copy `backend/.env.example` to `backend/.env`, then pick one option:

- **Local and offline:** install [Ollama](https://ollama.com), run `ollama pull llama3.2`, then set `OLLAMA_MODEL=llama3.2`.
- **Gemini free tier:** set `GEMINI_API_KEY`. The model name is configurable through `GEMINI_MODEL`, because Google retires model names over time.

The LLM never produces numbers. Every figure in its rewrite is checked against the agents' output, and the rewrite is discarded if anything differs.

---

## 3. Tests & CI

```bash
cd backend
pip install -r requirements-dev.txt
pytest -q          # 80 tests, ~15 s, fully offline
```

The tests cover:

- **Parsing:** profile extraction, negation and clarification.
- **Market data:** the live-data path, using a mocked yfinance with tz-aware prices, percentage D/E and a missing ticker.
- **Scoring:** bank-specific scoring and robustness to outliers.
- **Optimization rules:** optimizer constraints, glide path, whole-share execution and the validator feedback loop.
- **New agents:** HRP weights and caps, the MPT-vs-HRP bake-off and its fallback, both yfinance news formats, red-flag patterns, an LLM that can't invent a red flag, and an end-to-end run where a flagged stock is dropped by the critic loop.
- **Simulation:** Monte Carlo maths and Indian-rupee formatting.
- **LLM safety:** number verification, plus out-of-range LLM values being rejected.
- **End to end:** graph routing (clarify path, parallel joins, revision loop) and every API endpoint.

GitHub Actions (`.github/workflows/tests.yml`) runs the suite on Python 3.11–3.13 on every push.

---

## 4. API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, offline mode, active LLM |
| POST | `/api/analyze` | Run the graph and return the full result (or `needs_clarification` + questions) |
| POST | `/api/analyze/stream` | Same, as Server-Sent Events: one `step` event per agent, then `result` |
| GET | `/api/history?limit=20` | Recent analyses |
| GET | `/api/history/{id}` | One stored analysis |
| GET | `/api/graph` | Agent graph as Mermaid |

Request body:

```json
{
  "prompt": "I have 50000, I'm 20, moderate risk, long term",
  "amount": null, "monthly_sip": null, "age": null,
  "risk_tolerance": null, "horizon_years": null,
  "experience": null,
  "use_defaults": false
}
```

Structured fields override the text. `experience` is `"beginner"`, `"advanced"`, or `null` (auto-detect). `use_defaults: true` skips clarification and reports every assumption it made.

---

## 5. Methodology in brief

- **Policy allocation.** The risk bucket sets the starting mix. Two ceilings then cap equity: a glide path (horizon under 3 years → ≤20% stocks, under 5 → ≤40%, under 8 → ≤60%) and an age rule (≤ 110 − age %).
- **Expected returns.** Each stock's expected return is risk-free rate + β × (12% long-run market return − 6.5%) + a ±2% fundamental "view". Raw historical means extrapolate whatever the recent past did. This approach is in the spirit of Black-Litterman, kept simpler.
- **Risk.** The covariance matrix uses Ledoit-Wolf shrinkage. Weights are capped at 35% per stock and 40% per sector, and feasibility is checked before solving.
- **Objective.** Conservative → minimum volatility. Aggressive → maximum Sharpe ratio at a 6.5% risk-free rate. Moderate → the midpoint of the efficient frontier.
- **Portfolio risk.** Volatility is √(wᵀΣw) across asset classes, using the measured equity–gold correlation. It is not a linear sum.
- **Execution.** Weights become whole shares through `DiscreteAllocation`. Below ₹5,000 of equity budget, the stock portion goes to a Nifty 50 index fund instead.
- **Simulation.** Monthly Student-t steps (df = 5, fat tails), SIP contributions, and inflation-adjusted results.
- **Backtest.** Weights are re-optimized on data before the last year, then held through that unseen year and compared with Nifty 50 and an equal-weight version.
- **Strategy choice (MPT vs HRP).** Both are built from data before the last year and held through it. Higher Sharpe wins; if the two are within 0.05, the smaller maximum drawdown wins. HRP clusters stocks by correlation distance √((1−ρ)/2), orders them by the tree, then splits weight top-down in inverse proportion to each half's variance.
- **News.** Sentiment from −1 to +1 is shown but never used as a return view. Only severe red flags matter, and they act through Validator rule 9.

**Limitations:**

- Fundamentals use today's values, so the backtest has mild look-ahead bias.
- Bonds and cash are modelled with assumptions rather than market data.
- yfinance is an unofficial data source.
- Taxes and brokerage costs are not modelled yet.

See `CHANGES.md` for every fix made relative to v1, and why.

---

## 6. Project structure

```
whitefin/
├── backend/
│   ├── agents/               11 agents (one file each)
│   ├── tests/                pytest suite (offline)
│   ├── workflow.py           LangGraph graph: routing, parallel joins, critic loop, streaming
│   ├── main.py               FastAPI app (+ serves the frontend)
│   ├── config.py             every assumption in one place
│   ├── llm.py                optional Ollama / Gemini client
│   ├── database.py           SQLite: history + market-data cache
│   ├── models.py             typed request/response schemas
│   └── requirements*.txt
├── frontend/                 HTML/CSS/JS dashboard (Chart.js), no build step
├── .github/workflows/        CI
├── Dockerfile, docker-compose.yml, .dockerignore   (Hugging Face-ready, port 7860)
├── CHANGES.md                v1 → v2 change log
└── README.md
```

MIT licensed.
