# WhiteFIN v2 — Agentic AI Investment Assistant

WhiteFIN turns a plain-English request such as *"I have ₹1,00,000 and can add ₹5,000 a month. I'm 22, moderate risk, investing for 15 years"* into a researched, validated, explained portfolio. It is built entirely on free, open-source tools, and it runs with no API keys and no signup.

Nine agents cooperate through a **LangGraph state machine that makes decisions at runtime**. It doesn't just call functions in a fixed order:

- **It asks instead of guessing.** If your message doesn't say how much, how long or how much risk, the profiling agent routes to a clarification step and the UI asks you.
- **A critic revises the work.** The Validator Agent checks the portfolio against 8 rules. When a rule fails, it sends the portfolio back to the optimizer with concrete fixes, such as "drop MARUTI, cap share price at ₹2,667, hold 4 stocks." This loops until the portfolio passes or 3 attempts are used.
- **Independent agents run in parallel.** Fundamentals ∥ Risk, then Monte Carlo ∥ Backtest, with joins that wait for both branches.
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
     ┌────────┴────────┐  parallel
     ▼                 ▼
Fundamental        Risk Assessment
 Analysis          (vol, VaR, CVaR, drawdown, beta vs Nifty)
     └────────┬────────┘  join
              ▼
   ┌──► Portfolio Optimization ── glide path · CAPM returns + fundamental tilt
   │          │                    · sector caps · whole-share execution
   │          ▼
   └─── Validator ── 8 checks; on failure writes feedback for the next attempt
  revise      │ approved
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
| Portfolio Optimization | `agents/portfolio_optimization.py` | Asset mix, stock shortlist, weights, share quantities |
| Validator | `agents/validator.py` | Approve, or revise with specific feedback |
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
pytest -q          # 60 tests, ~12 s, fully offline
```

The tests cover:

- **Parsing:** profile extraction, negation and clarification.
- **Market data:** the live-data path, using a mocked yfinance with tz-aware prices, percentage D/E and a missing ticker.
- **Scoring:** bank-specific scoring and robustness to outliers.
- **Optimization rules:** optimizer constraints, glide path, whole-share execution and the validator feedback loop.
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
│   ├── agents/               9 agents (one file each)
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
├── Dockerfile, docker-compose.yml
├── CHANGES.md                v1 → v2 change log
└── README.md
```

MIT licensed.
