# Cohere (suggestions) and Groq (chat), over plain HTTP.
import json
import logging
import re

import requests

from ..config import settings

log = logging.getLogger(__name__)

COHERE_URL = "https://api.cohere.com/v2/chat"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
NOTE = ("You help one person in South Africa understand their own money. Amounts are in rand unless stated. "
        "Be specific: use their numbers. You are not a licensed financial adviser; say so briefly when advice is "
        "about buying or selling particular shares.")


class AIError(Exception):
    pass


def cohere(system: str, user: str, json_mode=False) -> str:
    if not settings.cohere_api_key:
        raise AIError("Cohere isn't set up yet (add the COHERE_API_KEY secret).")
    body = {"model": settings.cohere_model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(COHERE_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {settings.cohere_api_key}", "Content-Type": "application/json"})
    if not r.ok:
        log.warning("Cohere %s: %s", r.status_code, r.text[:300])
        raise AIError(f"Cohere didn't answer (HTTP {r.status_code}).")
    parts = (r.json().get("message") or {}).get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


_next_key = 0
_model = None
PREFERRED = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b",
             "openai/gpt-oss-20b", "llama-3.1-8b-instant"]


def groq_model(key, refresh=False):
    """The configured model, or (after a 'model not found') the best one this key can use."""
    global _model
    if _model and not refresh:
        return _model
    if not refresh:
        return settings.groq_model
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    except Exception:
        ids = []
    chat = [i for i in ids if not re.search(r"whisper|tts|guard|playai|distil|compound", i)]
    _model = next((p for p in PREFERRED if p in chat), chat[0] if chat else settings.groq_model)
    log.info("Groq model switched to %s", _model)
    return _model


def groq(messages, max_tokens=900, json_mode=False) -> str:
    """Chat completion; on a rate limit, the next key is tried."""
    global _next_key
    keys = settings.groq_api_keys
    if not keys:
        raise AIError("Groq isn't set up yet (add the GROQ_API_KEYS secret).")
    last = ""
    for i in range(len(keys)):
        key = keys[(_next_key + i) % len(keys)]
        body = {"messages": messages, "temperature": 0 if json_mode else 0.4, "max_tokens": max_tokens,
                **({"response_format": {"type": "json_object"}} if json_mode else {})}
        r = requests.post(GROQ_URL, timeout=60, headers={"Authorization": f"Bearer {key}"}, json={"model": groq_model(key), **body})
        if r.status_code in (400, 404) and "model" in r.text.lower():
            r = requests.post(GROQ_URL, timeout=60, headers={"Authorization": f"Bearer {key}"},
                              json={"model": groq_model(key, refresh=True), **body})
        if r.status_code == 429:
            last = "rate limited"
            continue
        if not r.ok:
            log.warning("Groq %s: %s", r.status_code, r.text[:300])
            raise AIError(f"Groq didn't answer (HTTP {r.status_code}).")
        _next_key = (_next_key + i + 1) % len(keys)
        return r.json()["choices"][0]["message"]["content"]
    raise AIError(f"Groq is busy ({last}); try again in a minute.")


def parse_json(text: str):
    text = text.strip()
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, AttributeError):
        return None
