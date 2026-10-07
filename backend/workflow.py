"""
workflow.py
------------
The agent graph (LangGraph). v1 was a straight line in which every agent ran
once, in a fixed order, and nothing could change the route. v2 makes
decisions at runtime:

                         ┌──────────────┐
             START ────► │ user_profiling│
                         └──────┬───────┘
              missing info?     │ (conditional edge)
          ┌──────── yes ────────┴───────── no ───────┐
          ▼                                          ▼
    ┌───────────┐                        ┌─────────────────────┐
    │  clarify  │──► END                 │ market_intelligence │
    │ (asks the │  (human-in-the-loop:   └─────────┬───────────┘
    │   user)   │   UI re-submits with       ┌─────┴──────┐   parallel fan-out
    └───────────┘   the answers)             ▼            ▼
                                   fundamental_analysis  risk_assessment
                                             └─────┬──────┘   join (waits for both)
                                                   ▼
                                   ┌──► portfolio_optimization
                       feedback    │               ▼
                    (exclude stock,│          validator ── approved? ──┐
                     tighten caps, │               │ no, fixable &      │ yes
                     cut equity)   └───────────────┘ attempts left      │
                                                         ┌──────────────┴──┐ parallel fan-out
                                                         ▼                 ▼
                                               scenario_simulation      backtest
                                                         └────────┬────────┘ join
                                                                  ▼
                                                           explainability ──► END

Agentic behaviours:
  1. Clarification gate — the graph refuses to guess required facts and asks.
  2. Critic loop — the validator's feedback changes what the optimizer does
     on its next attempt (generate -> critique -> revise).
  3. Parallel branches with joins — independent agents run concurrently.
  4. Every agent appends to a shared `trace`, which the API streams live.
"""
import operator
import time
from typing import Annotated, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from agents import (
    user_profiling, market_intelligence, fundamental_analysis, risk_assessment,
    portfolio_optimization, validator, scenario_simulation, backtest, explainability,
)
from config import MAX_OPTIMIZATION_ATTEMPTS


class WhiteFinState(TypedDict, total=False):
    prompt: str
    overrides: dict
    use_defaults: bool
    status: str
    questions: list
    user_profile: dict
    market_data: dict
    fundamentals: dict
    risk_metrics: dict
    attempt: int
    feedback: dict
    optimized_portfolio: dict
    validation: dict
    validation_history: Annotated[list, operator.add]   # reducer: parallel-safe append
    simulation: dict
    backtest: dict
    explanation: dict
    trace: Annotated[list, operator.add]


AGENT_LABELS = {
    "user_profiling": "User Profiling",
    "clarify": "Clarification",
    "market_intelligence": "Market Intelligence",
    "fundamental_analysis": "Fundamental Analysis",
    "risk_assessment": "Risk Assessment",
    "portfolio_optimization": "Portfolio Optimization",
    "validator": "Validator",
    "scenario_simulation": "Scenario Simulation",
    "backtest": "Backtest",
    "explainability": "Explainability",
}


def traced(name: str):
    """Times a node and appends a human-readable trace entry to the state."""
    def deco(fn):
        def wrapper(state: WhiteFinState) -> dict:
            t0 = time.perf_counter()
            update, message, details = fn(state)
            entry = {
                "agent": name, "label": AGENT_LABELS[name], "message": message,
                "details": details or {}, "duration_ms": round((time.perf_counter() - t0) * 1000),
                "timestamp": time.time(),
            }
            return {**update, "trace": [entry]}
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ------------------------------------------------------------------ nodes
@traced("user_profiling")
def node_user_profiling(state):
    p = user_profiling.run(state["prompt"], state.get("overrides", {}), state.get("use_defaults", False))
    if p["missing_required"]:
        msg = f"Couldn't find: {', '.join(p['missing_required'])} — will ask instead of guessing."
    else:
        bits = []
        if p["amount"]:
            bits.append(f"₹{p['amount']:,.0f}")
        if p["monthly_sip"]:
            bits.append(f"₹{p['monthly_sip']:,.0f}/month")
        msg = f"{' + '.join(bits)}, {p['risk_tolerance']}, {p['horizon_years']}y horizon, age {p['age']}."
    return {"user_profile": p}, msg, {"sources": p["field_sources"]}


@traced("clarify")
def node_clarify(state):
    p = state["user_profile"]
    questions = user_profiling.clarification_questions(p["missing_required"])
    return ({"status": "needs_clarification", "questions": questions},
            f"Asking {len(questions)} question(s) before building a portfolio.", {})


@traced("market_intelligence")
def node_market_intelligence(state):
    md = market_intelligence.run()
    msg = (f"{len(md['stocks'])} stocks + gold + benchmark loaded — {md['data_source'].replace('_', ' ')} "
           f"({md['live_ratio']:.0%} live{', from cache' if md['from_cache'] else ''}).")
    return {"market_data": md}, msg, {"notes": md["notes"]}


@traced("fundamental_analysis")
def node_fundamental_analysis(state):
    f = fundamental_analysis.run(state["market_data"])
    top = sorted(f.items(), key=lambda kv: -kv[1]["fundamental_score"])[:3]
    msg = "Top scores: " + ", ".join(f"{t.replace('.NS', '')} {v['fundamental_score']:.0f}" for t, v in top)
    return {"fundamentals": f}, msg, {}


@traced("risk_assessment")
def node_risk_assessment(state):
    r = risk_assessment.run(state["market_data"])
    vols = [v["annual_volatility"] for v in r.values() if v["annual_volatility"]]
    msg = f"Volatility, VaR, CVaR, drawdown and beta for {len(r)} stocks (vol range {min(vols):.0%}–{max(vols):.0%})."
    return {"risk_metrics": r}, msg, {}


@traced("portfolio_optimization")
def node_portfolio_optimization(state):
    attempt = state.get("attempt", 0) + 1
    feedback = state.get("feedback", {})
    opt = portfolio_optimization.run(state["user_profile"], state["market_data"], state["fundamentals"], feedback)
    b = opt["asset_class_allocation"]
    msg = (f"Attempt {attempt}: {b['stocks']:.0%} stocks / {b['bonds']:.0%} bonds / {b['gold']:.0%} gold / "
           f"{b['cash']:.0%} cash; {len(opt['equity_weights_within_stocks_sleeve'])} stocks via "
           f"{opt['equity_sleeve_performance']['method']}.")
    details = {"feedback_applied": feedback} if feedback else {}
    return {"optimized_portfolio": opt, "attempt": attempt}, msg, details


@traced("validator")
def node_validator(state):
    v = validator.run(state["user_profile"], state["optimized_portfolio"], state["attempt"], state.get("feedback", {}))
    passed = sum(c["passed"] for c in v["checks"])
    if v["verdict"] == "revise":
        msg = f"{passed}/{len(v['checks'])} checks passed — sending back: {'; '.join(v['failed'])}."
    elif v["verdict"] == "approved":
        msg = f"All {len(v['checks'])} checks passed — approved."
    else:
        msg = f"{passed}/{len(v['checks'])} passed; approved with warnings after {state['attempt']} attempt(s)."
    return ({"validation": v, "feedback": v["feedback"], "validation_history": [v]},
            msg, {"failed": v["failed"]})


@traced("scenario_simulation")
def node_scenario_simulation(state):
    p, opt = state["user_profile"], state["optimized_portfolio"]
    sim = scenario_simulation.run(
        initial_amount=p["amount"], monthly_sip=p["monthly_sip"],
        expected_annual_return=opt["blended_expected_annual_return"],
        annual_volatility=opt["blended_annual_volatility"], horizon_years=p["horizon_years"],
    )
    msg = (f"{sim['num_simulations']:,} paths: median ₹{sim['final_value_percentiles']['p50_median']:,.0f}, "
           f"P(loss) {sim['probability_of_loss']:.1%}.")
    return {"simulation": sim}, msg, {}


@traced("backtest")
def node_backtest(state):
    bt = backtest.run(state["optimized_portfolio"], state["market_data"], state["fundamentals"])
    if bt["available"]:
        msg = (f"Out-of-sample year: {bt['strategy']['total_return']:+.1%} vs "
               f"{bt['benchmark']['total_return']:+.1%} benchmark.")
    else:
        msg = "Skipped: " + bt["reason"]
    return {"backtest": bt}, msg, {}


@traced("explainability")
def node_explainability(state):
    flat = [c for v in state.get("validation_history", []) for c in v["checks"] if not c["passed"]]
    exp = explainability.run({**state, "validation_history_flat": flat})
    if exp["used_llm"]:
        how = f"rewritten by {exp['llm_provider']}, numbers verified"
    elif exp["llm_rejected_reason"]:
        how = "LLM rewrite rejected (changed numbers) — template used"
    else:
        how = "template engine (no LLM configured)"
    return ({"explanation": exp, "status": "complete"},
            f"{len(exp['sections'])} sections; {how}. Data quality {exp['data_quality_score']}/100.", {})


# ------------------------------------------------------------------ routing
def route_after_profile(state) -> str:
    return "clarify" if state["user_profile"]["missing_required"] else "market_intelligence"


def route_after_validation(state):
    if state["validation"]["verdict"] == "revise" and state["attempt"] < MAX_OPTIMIZATION_ATTEMPTS:
        return "portfolio_optimization"
    return ["scenario_simulation", "backtest"]


def build_graph():
    g = StateGraph(WhiteFinState)
    g.add_node("user_profiling", node_user_profiling)
    g.add_node("clarify", node_clarify)
    g.add_node("market_intelligence", node_market_intelligence)
    g.add_node("fundamental_analysis", node_fundamental_analysis)
    g.add_node("risk_assessment", node_risk_assessment)
    g.add_node("portfolio_optimization", node_portfolio_optimization)
    g.add_node("validator", node_validator)
    g.add_node("scenario_simulation", node_scenario_simulation)
    g.add_node("backtest", node_backtest)
    g.add_node("explainability", node_explainability)

    g.add_edge(START, "user_profiling")
    g.add_conditional_edges("user_profiling", route_after_profile, ["clarify", "market_intelligence"])
    g.add_edge("clarify", END)
    g.add_edge("market_intelligence", "fundamental_analysis")
    g.add_edge("market_intelligence", "risk_assessment")
    g.add_edge(["fundamental_analysis", "risk_assessment"], "portfolio_optimization")
    g.add_edge("portfolio_optimization", "validator")
    g.add_conditional_edges("validator", route_after_validation,
                            ["portfolio_optimization", "scenario_simulation", "backtest"])
    g.add_edge(["scenario_simulation", "backtest"], "explainability")
    g.add_edge("explainability", END)
    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def _initial(prompt: str, overrides: Optional[dict], use_defaults: bool) -> dict:
    return {"prompt": prompt, "overrides": overrides or {}, "use_defaults": use_defaults,
            "trace": [], "validation_history": []}


def run_pipeline(prompt: str, overrides: Optional[dict] = None, use_defaults: bool = False) -> dict:
    return get_graph().invoke(_initial(prompt, overrides, use_defaults))


REDUCED_KEYS = ("trace", "validation_history")


def stream_pipeline(prompt: str, overrides: Optional[dict] = None, use_defaults: bool = False):
    """Yields ("step", trace_entry) as each agent finishes, then ("done", final_state)."""
    state = _initial(prompt, overrides, use_defaults)
    for chunk in get_graph().stream(state, stream_mode="updates"):
        for _node, update in chunk.items():
            if not update:
                continue
            for k, v in update.items():
                if k in REDUCED_KEYS:
                    state[k] = state.get(k, []) + v
                else:
                    state[k] = v
            for entry in update.get("trace", []):
                yield "step", entry
    yield "done", state
