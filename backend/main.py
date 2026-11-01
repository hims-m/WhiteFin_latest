"""
main.py
--------
FastAPI entry point.

    GET  /api/health              status, data mode, which LLM (if any) is active
    POST /api/analyze             run the agent graph, return the full result
    POST /api/analyze/stream      same, but streams each agent's step live (Server-Sent Events)
    GET  /api/history             recent analyses (lightweight list)
    GET  /api/history/{id}        one stored analysis in full
    GET  /api/graph               the agent graph as a Mermaid diagram

The frontend is also served at http://127.0.0.1:8000/ so one command runs everything.

Run:  uvicorn main:app --reload --port 8000
"""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import llm
from config import BACKEND_PORT, CORS_ORIGINS, OFFLINE_MODE, STOCK_UNIVERSE
from database import init_db, save_analysis, get_history, get_analysis
from models import AnalyzeRequest, AnalyzeResponse, HistoryItem
from workflow import run_pipeline, stream_pipeline, get_graph

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("whitefin.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):  # replaces the deprecated @app.on_event("startup")
    init_db()
    yield


app = FastAPI(
    title="WhiteFIN API",
    description="Agentic AI investment assistant — educational project, not investment advice.",
    version="2.1.0",
    lifespan=lifespan,
)

# No cookies/auth are used, so credentials are off. (v1 combined allow_origins=["*"]
# with allow_credentials=True, which effectively lets any website make
# credentialed requests.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ------------------------------------------------------------------ helpers
def _default(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, (pd.Series, np.ndarray)):
        return o.tolist()
    return str(o)


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=_default))


def _slim_market_data(md: dict) -> dict:
    def slim(s: dict) -> dict:
        recent = s["history"].tail(120)
        return {
            "ticker": s.get("ticker"), "name": s.get("name"), "sector": s.get("sector"),
            "price": round(float(s["price"]), 2), "source": s.get("source"),
            "info_source": s.get("info_source"),
            "recent_prices": [round(float(v), 2) for v in recent.values],
            "recent_dates": [d.strftime("%Y-%m-%d") for d in recent.index],
        }
    return {
        "data_source": md["data_source"], "live_ratio": md["live_ratio"],
        "from_cache": md["from_cache"], "notes": md["notes"],
        "benchmark": {"name": md["benchmark"]["name"], "source": md["benchmark"]["source"]},
        "stocks": {t: slim(s) for t, s in md["stocks"].items()},
        "gold": slim(md["gold"]),
    }


def _build_result(state: dict) -> dict:
    profile = {k: v for k, v in state["user_profile"].items() if k != "raw_prompt"}
    result = {
        "status": state.get("status", "complete"),
        "user_profile": profile,
        "questions": state.get("questions", []),
        "trace": state.get("trace", []),
    }
    if result["status"] == "complete":
        result.update({
            "market_data": _slim_market_data(state["market_data"]),
            "fundamentals": state["fundamentals"],
            "risk_metrics": state["risk_metrics"],
            "optimized_portfolio": state["optimized_portfolio"],
            "validation": state["validation"],
            "validation_history": state.get("validation_history", []),
            "simulation": state["simulation"],
            "backtest": state["backtest"],
            "news": state.get("news"),
            "strategy": state.get("strategy"),
            "explanation": state["explanation"],
        })
    result = _jsonable(result)
    if result["status"] == "complete":
        result["id"] = save_analysis(state["prompt"], profile, result)
    return result


def _overrides(req: AnalyzeRequest) -> dict:
    fields = ("amount", "monthly_sip", "age", "risk_tolerance", "horizon_years", "experience")
    return {f: getattr(req, f) for f in fields if getattr(req, f) is not None}


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return {
        "status": "ok", "service": "WhiteFIN API", "version": app.version,
        "offline_mode": OFFLINE_MODE, "universe_size": len(STOCK_UNIVERSE),
        "llm_provider": llm.provider(),
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    try:
        state = run_pipeline(req.prompt, _overrides(req), req.use_defaults)
        return _build_result(state)
    except Exception:
        log.exception("pipeline failed")
        raise HTTPException(status_code=500, detail="The analysis failed. Check the server log for details.")


@app.post("/api/analyze/stream")
def analyze_stream(req: AnalyzeRequest):
    """Server-Sent Events: `event: step` per agent, then `event: result` (or `event: error`)."""
    def events():
        try:
            for kind, payload in stream_pipeline(req.prompt, _overrides(req), req.use_defaults):
                if kind == "step":
                    yield f"event: step\ndata: {json.dumps(_jsonable(payload))}\n\n"
                else:
                    yield f"event: result\ndata: {json.dumps(_build_result(payload))}\n\n"
        except Exception:
            log.exception("pipeline failed (stream)")
            yield f"event: error\ndata: {json.dumps({'detail': 'The analysis failed. Check the server log.'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/history", response_model=list[HistoryItem])
def history(limit: int = Query(20, ge=1, le=100)):
    return get_history(limit)


@app.get("/api/history/{analysis_id}")
def history_item(analysis_id: int):
    item = get_analysis(analysis_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return item


@app.get("/api/graph")
def graph():
    return {"mermaid": get_graph().get_graph().draw_mermaid()}


# Serve the dashboard from the same server (mounted last so /api routes win).
_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=BACKEND_PORT, reload=True)
