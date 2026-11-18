"""
JURIES PROOF - LLM provider abstraction (Gemini / OpenAI).

The LLM is an OPTIONAL enhancement layer. When no key is configured, every
feature falls back to extractive answers built strictly from retrieved
evidence. The LLM is never allowed to answer without evidence: prompts
constrain it to the supplied context only.
"""
import json
import os
import urllib.request

from . import config


class LLMUnavailable(Exception):
    pass


def _call_gemini(prompt: str, system: str = "") -> str:
    if not config.GEMINI_API_KEY:
        raise LLMUnavailable("Gemini not configured")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": (system + "\n\n" + prompt).strip()}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode())
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise LLMUnavailable("Empty Gemini response")


def _call_openai(prompt: str, system: str = "") -> str:
    if not config.OPENAI_API_KEY:
        raise LLMUnavailable("OpenAI not configured")
    body = {
        "model": config.OPENAI_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode())
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise LLMUnavailable("Empty OpenAI response")


def generate(prompt: str, system: str = "") -> str:
    """Try configured providers in order; raise LLMUnavailable if none work."""
    errors = []
    for fn in (_call_gemini, _call_openai):
        try:
            return fn(prompt, system)
        except LLMUnavailable as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{fn.__name__}: {type(e).__name__}")
    raise LLMUnavailable("; ".join(errors))


GROUNDING_SYSTEM = (
    "You are JURIES PROOF, an evidence-first legal intelligence assistant. "
    "Answer ONLY using the numbered evidence excerpts provided. "
    "Cite the evidence number for every factual statement, e.g. [E1]. "
    "If the evidence is insufficient to answer the question, reply exactly: "
    "INSUFFICIENT EVIDENCE. Never invent names, dates, amounts, sections, "
    "citations or facts that are not in the evidence."
)
