"""
models.py
----------
Typed request/response schemas. v1 typed every response field as a bare
`dict`, so /docs showed nothing useful and bad input reached the agents.
Now inputs are range-checked and the main outputs have real shapes.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

RiskLevel = Literal["conservative", "moderate", "aggressive"]
Experience = Literal["beginner", "advanced"]


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=2000, examples=[
        "I have ₹50,000 to invest. I'm 20, moderate risk, long-term growth."
    ])
    # Optional structured answers (from the form or from clarification
    # questions). They always win over what is parsed from the prompt.
    amount: Optional[float] = Field(None, ge=0, le=1e10)
    monthly_sip: Optional[float] = Field(None, ge=0, le=1e8)
    age: Optional[int] = Field(None, ge=10, le=100)
    risk_tolerance: Optional[RiskLevel] = None
    horizon_years: Optional[int] = Field(None, ge=1, le=60)
    # How the results are explained (never changes the portfolio). Omit to auto-detect.
    experience: Optional[Experience] = None
    # "Just use sensible defaults" — skips the clarification step.
    use_defaults: bool = False


class TraceEvent(BaseModel):
    agent: str
    label: str
    message: str
    details: dict[str, Any] = {}
    duration_ms: int
    timestamp: float


class Question(BaseModel):
    field: str
    type: Literal["number", "choice"]
    question: str
    placeholder: Optional[str] = None
    options: Optional[list[dict[str, str]]] = None


class UserProfile(BaseModel):
    amount: Optional[float]
    monthly_sip: float
    age: Optional[int]
    risk_tolerance: Optional[RiskLevel]
    horizon_years: Optional[int]
    goal: str
    experience: Experience = "beginner"
    field_sources: dict[str, str]
    missing_required: list[str]
    assumptions: list[str]


class ValidationCheck(BaseModel):
    check: str
    passed: bool
    detail: str
    fix: Optional[str] = None


class ValidationReport(BaseModel):
    verdict: Literal["approved", "revise", "approved_with_warnings"]
    attempt: int
    checks: list[ValidationCheck]
    failed: list[str]
    warnings: list[str]


class StockCard(BaseModel):
    ticker: str
    short: str
    name: str
    sector: Optional[str]
    weight_in_sleeve: float
    weight_total: float
    score: Optional[float]
    buy: str
    pros: list[str]
    cons: list[str]
    metrics: dict[str, str] = {}


class ExplanationSection(BaseModel):
    id: str
    title: str
    lead: str = ""
    points: list[str] = []
    cards: list[StockCard] = []


class Explanation(BaseModel):
    level: Experience
    overview: dict[Experience, str]                       # "in a nutshell", per level
    levels: dict[Experience, list[ExplanationSection]]    # both versions, so the UI can switch
    sections: list[ExplanationSection]                    # the user's level
    summary: str
    used_llm: bool
    llm_provider: Optional[str]
    llm_rejected_reason: Optional[str]
    data_quality_score: int
    disclaimer: str


class AnalyzeResponse(BaseModel):
    status: Literal["complete", "needs_clarification"]
    id: Optional[int] = None
    user_profile: UserProfile
    questions: list[Question] = []
    trace: list[TraceEvent] = []
    # Present when status == "complete"
    market_data: Optional[dict[str, Any]] = None
    fundamentals: Optional[dict[str, Any]] = None
    risk_metrics: Optional[dict[str, Any]] = None
    optimized_portfolio: Optional[dict[str, Any]] = None
    validation: Optional[ValidationReport] = None
    validation_history: list[ValidationReport] = []
    simulation: Optional[dict[str, Any]] = None
    backtest: Optional[dict[str, Any]] = None
    news: Optional[dict[str, Any]] = None        # News & Sentiment Agent
    strategy: Optional[dict[str, Any]] = None    # Strategy Selector Agent (MPT vs HRP)
    explanation: Optional[Explanation] = None


class HistoryItem(BaseModel):
    id: int
    created_at: str
    prompt: Optional[str]
    user_profile: dict[str, Any]
