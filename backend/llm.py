"""
llm.py
-------
One tiny, dependency-free client for the OPTIONAL language models WhiteFIN
can use. Order of preference:

  1. Ollama (local, free, offline)      — if OLLAMA_MODEL is set
  2. Google Gemini (free-tier API key)  — if GEMINI_API_KEY is set
  3. None                               — every caller has a rule-based fallback

Design rule for the whole project: the LLM is used for *language*
(understanding the user's words, rewording explanations). It never produces
the numbers in a recommendation — those always come from the deterministic
finance agents, and the Explainability Agent verifies this.
"""
import json
import logging
import urllib.request

from config import (
    OLLAMA_URL, OLLAMA_MODEL, GEMINI_API_KEY, GEMINI_MODEL, LLM_TIMEOUT_SECONDS,
)

log = logging.getLogger("whitefin.llm")


def provider() -> str | None:
    if OLLAMA_MODEL:
        return f"ollama:{OLLAMA_MODEL}"
    if GEMINI_API_KEY:
        return f"gemini:{GEMINI_MODEL}"
    return None


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode())


def complete(prompt: str, json_mode: bool = False) -> str | None:
    """Returns the model's text, or None if no LLM is configured or the call fails."""
    try:
        if OLLAMA_MODEL:
            payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.2}}
            if json_mode:
                payload["format"] = "json"
            data = _post_json(f"{OLLAMA_URL.rstrip('/')}/api/generate", payload)
            return data.get("response")

        if GEMINI_API_KEY:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{GEMINI_MODEL}:generateContent")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2},
            }
            if json_mode:
                payload["generationConfig"]["responseMimeType"] = "application/json"
            # Key goes in a header, not the URL, so it doesn't end up in logs.
            data = _post_json(url, payload, headers={"x-goog-api-key": GEMINI_API_KEY})
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:  # network error, retired model name, quota, bad JSON...
        log.warning("LLM call failed (%s): %s", provider(), exc)
    return None


def complete_json(prompt: str) -> dict | None:
    text = complete(prompt, json_mode=True)
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])
    except Exception as exc:
        log.warning("LLM returned non-JSON output: %s", exc)
        return None
