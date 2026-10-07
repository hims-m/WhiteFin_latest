"""
database.py
------------
SQLite storage via SQLAlchemy (swap DATABASE_URL for Postgres/Supabase later;
the ORM code is unchanged).

Two tables:
  * analysis_history — every completed analysis (for the History panel)
  * market_cache     — fetched market data with a timestamp, so we don't hit
                       Yahoo Finance (slow, rate-limited) on every request
"""
import json
import datetime as dt

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATABASE_URL, MARKET_CACHE_TTL_HOURS

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _now() -> dt.datetime:
    # timezone-aware UTC (datetime.utcnow() is deprecated); stored naive for SQLite
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class AnalysisHistory(Base):
    __tablename__ = "analysis_history"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_now)
    prompt = Column(Text, nullable=True)
    user_profile_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)


class MarketCache(Base):
    __tablename__ = "market_cache"

    key = Column(String(200), primary_key=True)
    fetched_at = Column(DateTime, default=_now)
    payload = Column(Text, nullable=False)


def init_db():
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------- history
def save_analysis(prompt: str, user_profile: dict, result: dict) -> int:
    with SessionLocal() as db:
        row = AnalysisHistory(
            prompt=prompt,
            user_profile_json=json.dumps(user_profile, default=str),
            result_json=json.dumps(result, default=str),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def get_history(limit: int = 20) -> list[dict]:
    """Lightweight list for the sidebar (no full result payloads)."""
    with SessionLocal() as db:
        rows = (
            db.query(AnalysisHistory)
            .order_by(AnalysisHistory.created_at.desc())
            .limit(limit)
            .all()
        )
        out = []
        for r in rows:
            profile = json.loads(r.user_profile_json)
            out.append({
                "id": r.id,
                "created_at": r.created_at.isoformat() + "Z",
                "prompt": r.prompt,
                "user_profile": profile,
            })
        return out


def get_analysis(analysis_id: int) -> dict | None:
    with SessionLocal() as db:
        r = db.get(AnalysisHistory, analysis_id)
        if r is None:
            return None
        result = json.loads(r.result_json)
        result["id"] = r.id
        return result


# ---------------------------------------------------------------- cache
def cache_get(key: str, ttl_hours: float = MARKET_CACHE_TTL_HOURS):
    with SessionLocal() as db:
        row = db.get(MarketCache, key)
        if row is None:
            return None
        age = _now() - row.fetched_at
        if age > dt.timedelta(hours=ttl_hours):
            return None
        return json.loads(row.payload)


def cache_set(key: str, value) -> None:
    with SessionLocal() as db:
        row = db.get(MarketCache, key)
        payload = json.dumps(value, default=str)
        if row is None:
            db.add(MarketCache(key=key, payload=payload, fetched_at=_now()))
        else:
            row.payload = payload
            row.fetched_at = _now()
        db.commit()
