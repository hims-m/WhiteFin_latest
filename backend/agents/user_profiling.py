"""
User Profiling Agent
---------------------
Turns free text (+ optional form overrides) into a structured investor profile
AND tells the rest of the graph what it does NOT know yet.

    {
      "amount": 50000.0, "monthly_sip": 0.0, "age": 20,
      "risk_tolerance": "moderate", "horizon_years": 15, "goal": "wealth growth",
      "field_sources": {"amount": "text", "age": "text", "horizon_years": "inferred", ...},
      "missing_required": [],          # -> the workflow asks the user instead of guessing
      "assumptions": ["..."],          # anything we filled in, said out loud
    }

How it reads text (in order):
  1. Form overrides always win (source = "form").
  2. A rule-based parser classifies every number by its context: "20 years old"
     is an age, "for 10 years" is a horizon, "₹5,000 per month" is a SIP,
     "1.5 lakh" is an amount, "I'm 20" is an age. Risk words are checked for
     negation ("I don't want high risk" -> conservative).
  3. If an LLM is configured and something required is still missing, the LLM
     gets one try at extracting it as JSON; its answer is range-checked
     before use (source = "llm").
  4. Anything still missing is either asked (clarification) or, if the user
     chose "use defaults", filled with a default that is reported back.
"""
import math
import re

import llm

RISK_LEVELS = ("conservative", "moderate", "aggressive")
REQUIRED_FIELDS = ("amount", "risk_tolerance", "horizon_years")

RISK_PHRASES = {
    "conservative": [
        "conservative", "safe", "safety", "low risk", "low-risk", "cautious", "no risk",
        "risk averse", "risk-averse", "capital protection", "preserve my capital",
    ],
    "aggressive": [
        "aggressive", "high risk", "high-risk", "risky", "maximum growth", "max growth",
        "high returns", "very high returns",
    ],
    "moderate": ["moderate", "medium risk", "medium-risk", "balanced", "some risk"],
}
NEGATORS = r"(?:not|no|don't|dont|do not|never|avoid|without|can't|cannot|hate|nothing)"
POSITIVE_NEGATIONS = r"(?:don't|dont|do not)\s+mind|(?:okay|ok|fine|comfortable)\s+with"

GOAL_PATTERNS = [
    ("retirement", r"\bretire\w*|\bpension\b"),
    ("house", r"\bhouse\b|\bhome\b|\bproperty\b|\bflat\b|\bapartment\b"),
    ("education", r"\beducation\b|\bcollege\b|\bmasters?\b|\bmba\b|\bschool fees\b|\bstud(?:y|ies)\b"),
    ("short-term goal", r"\bwedding\b|\bcar\b|\bbike\b|\bvacation\b|\btrip\b|\bshort[- ]term\b"),
    ("emergency fund", r"\bemergency\b"),
    ("wealth growth", r"\bgrow\w*|\bwealth\b"),
]

_NUM_RE = re.compile(
    r"(?P<cur>₹|rs\.?\s*|inr\s*|\$)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>lakhs?\b|lacs?\b|crores?\b|cr\b|k\b|thousand\b|l\b)?"
    r"(?P<pct>\s*%)?",
    re.IGNORECASE,
)
_UNIT_MULT = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5,
              "crore": 1e7, "crores": 1e7, "cr": 1e7, "k": 1e3, "thousand": 1e3}
_MONTHLY_AFTER = re.compile(r"^\s*(?:rupees|rs\.?|inr)?\s*(?:per month|/\s*month|a month|every month|monthly|each month|pm\b|p\.m\.)")
_MONTHLY_BEFORE = re.compile(r"(?:sip of|sip|monthly|every month|each month)\s*(?:of|is)?\s*(?:₹|rs\.?|inr)?\s*$")


def _normalize(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").lower()


def _parse_numbers(text: str) -> dict:
    """Classifies every number in the text by context. Returns the first value
    found for each slot."""
    found: dict = {}
    for m in _NUM_RE.finditer(text):
        if m.group("pct"):
            continue
        raw = m.group("num").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        unit = (m.group("unit") or "").lower()
        cur = m.group("cur")
        head = text[max(0, m.start() - 25):m.start()]
        tail = text[m.end():m.end() + 30]

        if re.match(r"\s*(?:years?|yrs?)\s*old|\s*y/?o\b|\s*(?:years?|yrs?)\s*of\s*age", tail):
            found.setdefault("age", value)
        elif not unit and not cur and re.match(r"\s*(?:years?|yrs?|y)\b", tail):
            found.setdefault("horizon_years", value)
        elif not unit and not cur and re.match(r"\s*months?\b", tail):
            found.setdefault("horizon_years", max(1, math.ceil(value / 12)))
        elif re.search(r"retire\w*\s+(?:at|by)\s*(?:the age of|age)?\s*$", head):
            found.setdefault("retire_age", value)
        elif unit or cur or value >= 100 or re.match(r"\s*(?:rupees|rs\b|inr)", tail):
            amount = value * _UNIT_MULT.get(unit, 1)
            if _MONTHLY_AFTER.search(tail[:30]) or _MONTHLY_BEFORE.search(head):
                found.setdefault("monthly_sip", amount)
            else:
                found.setdefault("amount", amount)
        elif re.search(r"(?:i'm|i am|im|age|aged|age is|age of)\s*$", head) and 10 <= value <= 100:
            found.setdefault("age", value)
    return found


def _extract_risk(text: str) -> str | None:
    """Collects every risk phrase, flips negated ones, and — if the text names
    more than one level ("moderate to aggressive") — picks the more cautious."""
    votes = []
    for level, phrases in RISK_PHRASES.items():
        for phrase in phrases:
            for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", text):
                head = text[max(0, m.start() - 30):m.start()]
                window = " ".join(head.split()[-3:])
                negated = (
                    phrase != "no risk"
                    and re.search(r"\b" + NEGATORS + r"\b", window)
                    and not re.search(POSITIVE_NEGATIONS, head)
                )
                if not negated:
                    votes.append(level)
                elif level == "aggressive":
                    votes.append("moderate" if re.search(r"\btoo\b", window) else "conservative")
                else:  # "not conservative", "don't want to play safe"
                    votes.append("moderate")
    if not votes:
        return None
    return min(votes, key=RISK_LEVELS.index)


def _extract_goal(text: str) -> tuple[str, bool]:
    for goal, pattern in GOAL_PATTERNS:
        if re.search(pattern, text):
            return goal, True
    return "wealth growth", False


def _horizon_from_words(text: str, goal: str) -> int | None:
    if re.search(r"long[- ]term|long run", text):
        return 15
    if re.search(r"medium[- ]term", text):
        return 7
    if re.search(r"short[- ]term", text) or goal == "short-term goal":
        return 3
    if goal == "emergency fund":
        return 1
    return None


BEGINNER_CUES = re.compile(
    r"\b(?:beginner|newbie|novice|new to (?:investing|stocks|the market|this)|first[- ]time|"
    r"never invested|no idea|don't know much|dont know much|just starting|explain simply|in simple terms)\b")
JARGON = [
    r"\bsharpe\b", r"\bbeta\b", r"\bcagr\b", r"\bp/?e\b", r"\bdrawdown\b", r"\bvolatility\b",
    r"\balpha\b", r"\brebalanc\w*", r"\basset allocation\b", r"\befficient frontier\b", r"\bvar\b",
    r"\bsortino\b", r"\bexpense ratio\b", r"\bxirr\b", r"\b(?:small|mid|large)[- ]?cap\b",
    r"\bcorrelation\b", r"\bduration\b", r"\bderivatives?\b", r"\bfactor\b", r"\bstd\b",
]


def _extract_experience(text: str) -> str | None:
    """Beginner if the user says so; experienced if they use >= 2 investing terms."""
    if BEGINNER_CUES.search(text):
        return "beginner"
    if sum(bool(re.search(p, text)) for p in JARGON) >= 2:
        return "advanced"
    return None


def _llm_extract(prompt: str, fields: list[str]) -> dict:
    """One LLM attempt at the fields rules couldn't find. Values are validated."""
    if not llm.provider() or not fields:
        return {}
    ask = (
        "Extract investment details from the user's message. Reply with ONLY a JSON object "
        "with these keys (use null when the message does not say):\n"
        '  "amount": lump-sum rupees as a number,\n'
        '  "monthly_sip": monthly contribution in rupees as a number,\n'
        '  "age": integer,\n'
        '  "risk_tolerance": one of "conservative", "moderate", "aggressive",\n'
        '  "horizon_years": integer number of years the money stays invested.\n'
        "Convert lakh=100000 and crore=10000000. Respect negation: \"I don't want high risk\" "
        "is conservative. Never guess.\n\nMessage: " + prompt
    )
    data = llm.complete_json(ask) or {}
    clean = {}
    try:
        if "amount" in fields and data.get("amount") and 0 < float(data["amount"]) < 1e11:
            clean["amount"] = float(data["amount"])
        if data.get("monthly_sip") and 0 < float(data["monthly_sip"]) < 1e9:
            clean["monthly_sip"] = float(data["monthly_sip"])
        if "age" in fields and data.get("age") and 10 <= int(data["age"]) <= 100:
            clean["age"] = int(data["age"])
        if "risk_tolerance" in fields and str(data.get("risk_tolerance", "")).lower() in RISK_LEVELS:
            clean["risk_tolerance"] = str(data["risk_tolerance"]).lower()
        if "horizon_years" in fields and data.get("horizon_years") and 1 <= int(data["horizon_years"]) <= 60:
            clean["horizon_years"] = int(data["horizon_years"])
    except (TypeError, ValueError):
        pass
    return clean


def clarification_questions(missing: list[str]) -> list[dict]:
    bank = {
        "amount": {"field": "amount", "type": "number",
                   "question": "How much do you want to invest as a lump sum (₹)? Enter 0 if you'll only invest monthly.",
                   "placeholder": "e.g. 50000"},
        "monthly_sip": {"field": "monthly_sip", "type": "number",
                        "question": "How much can you add every month (₹)? Enter 0 for none.",
                        "placeholder": "e.g. 5000"},
        "risk_tolerance": {"field": "risk_tolerance", "type": "choice",
                           "question": "If your portfolio fell 20% in a bad year, what would you do?",
                           "options": [
                               {"value": "conservative", "label": "Sell — I can't afford big drops (Conservative)"},
                               {"value": "moderate", "label": "Wait it out — some ups and downs are fine (Moderate)"},
                               {"value": "aggressive", "label": "Invest more — drops are buying opportunities (Aggressive)"},
                           ]},
        "horizon_years": {"field": "horizon_years", "type": "number",
                          "question": "In how many years will you need this money?",
                          "placeholder": "e.g. 10"},
        "age": {"field": "age", "type": "number", "question": "How old are you?", "placeholder": "e.g. 22"},
    }
    return [bank[f] for f in missing if f in bank]


def run(prompt: str, overrides: dict, use_defaults: bool = False) -> dict:
    text = _normalize(prompt)
    parsed = _parse_numbers(text)
    risk_text = _extract_risk(text)
    goal, goal_stated = _extract_goal(text)

    values: dict = {}
    sources: dict = {}

    def put(field, value, source):
        if field not in values and value is not None:
            values[field] = value
            sources[field] = source

    for field in ("amount", "monthly_sip", "age", "risk_tolerance", "horizon_years"):
        if overrides.get(field) is not None:
            put(field, overrides[field], "form")

    put("amount", parsed.get("amount"), "text")
    put("monthly_sip", parsed.get("monthly_sip"), "text")
    if parsed.get("age") and 10 <= parsed["age"] <= 100:
        put("age", int(parsed["age"]), "text")
    put("risk_tolerance", risk_text, "text")
    if parsed.get("horizon_years") and 1 <= parsed["horizon_years"] <= 60:
        put("horizon_years", int(parsed["horizon_years"]), "text")
    if parsed.get("retire_age") and "age" in values and parsed["retire_age"] > values["age"]:
        put("horizon_years", int(parsed["retire_age"] - values["age"]), "text")

    # Fuzzy cases: let the LLM try the gaps (if one is configured)
    gaps = [f for f in ("amount", "age", "risk_tolerance", "horizon_years") if f not in values]
    for field, value in _llm_extract(prompt, gaps).items():
        put(field, value, "llm")

    put("horizon_years", _horizon_from_words(text, goal), "inferred")

    # A monthly-only investor (SIP, no lump sum) is a complete answer.
    if "amount" not in values and values.get("monthly_sip"):
        put("amount", 0.0, "inferred")

    missing_required = [f for f in REQUIRED_FIELDS if f not in values]

    # Experience level only changes HOW results are explained, never the portfolio.
    if overrides.get("experience") in ("beginner", "advanced"):
        experience, exp_source = overrides["experience"], "form"
    else:
        detected = _extract_experience(text)
        experience, exp_source = (detected, "text") if detected else ("beginner", "default")

    assumptions = []
    if use_defaults or not missing_required:
        if "age" not in values:
            put("age", 30, "default")
            assumptions.append("Age wasn't mentioned, so we assumed 30.")
        defaults = {
            "amount": (50_000.0, "Investment amount wasn't mentioned, so we used ₹50,000."),
            "risk_tolerance": ("moderate", "Risk appetite wasn't mentioned, so we used 'moderate'."),
            "horizon_years": (max(1, min(15, 65 - int(values["age"]))),
                              "Time horizon wasn't mentioned, so we used {v} years."),
        }
        for field in missing_required:
            value, note = defaults[field]
            put(field, value, "default")
            assumptions.append(note.format(v=value))
        missing_required = []
    if sources.get("horizon_years") == "inferred":
        assumptions.append(f"We read your time frame as about {values['horizon_years']} years.")

    risk = str(values.get("risk_tolerance") or "").lower() or None
    if risk is not None and risk not in RISK_LEVELS:
        risk = "moderate"
        sources["risk_tolerance"] = "default"
        assumptions.append("Unrecognised risk level — used 'moderate'.")

    profile = {
        "amount": round(float(values["amount"]), 2) if "amount" in values else None,
        "monthly_sip": round(float(values.get("monthly_sip") or 0.0), 2),
        "age": int(values["age"]) if "age" in values else None,
        "risk_tolerance": risk,
        "horizon_years": int(values["horizon_years"]) if "horizon_years" in values else None,
        "goal": goal,
        "goal_stated": goal_stated,
        "experience": experience,
        "field_sources": sources,
        "missing_required": missing_required,
        "assumptions": assumptions,
        "raw_prompt": prompt,
    }
    if "monthly_sip" not in sources:
        profile["field_sources"]["monthly_sip"] = "none"
    profile["field_sources"]["experience"] = exp_source
    return profile
