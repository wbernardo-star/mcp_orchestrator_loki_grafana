#app/intent_service.py via LLM

"""
Internal LLM-based Intent Service for MCP Orchestrator.

Responsibility:
  - Take user's latest text (and some context if needed)
  - Ask an LLM to classify it into a small set of intents
  - Return a structured result: intent + confidence

This keeps all the phrase-matching logic OUT of main.py.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, List

from openai import OpenAI

from .logging_loki import loki


# ------------- Config -------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
INTENT_MODEL = os.getenv("INTENT_MODEL", "gpt-4o-mini")

if not OPENAI_API_KEY:
    # We won't raise here, but calls will fail; Loki will record the errors.
    pass

_openai_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


# ------------- Data model -------------


@dataclass
class IntentResult:
    intent: str
    confidence: float
    raw_json: dict


# ------------- Core classifier -------------


def classify_intent(
    text: str,
    user_id: str,
    channel: str,
    session_id: str,
    history: Optional[List[str]] = None,
) -> IntentResult:
    """
    Use OpenAI Chat Completions to classify the user's message
    into a small, fixed set of intents.

    Intents (v1):
      - "menu"      → user wants to see/hear the menu
      - "order"     → user wants to place an order
      - "greeting"  → "hello", "hi", "hey"
      - "smalltalk" → chit-chat not related to food logic
      - "unknown"   → anything else

    Returns:
      IntentResult(intent=..., confidence=..., raw_json=...)
    """
    start = time.perf_counter()

    # ------- Default result in case LLM fails --------
    default_result = IntentResult(
        intent="unknown",
        confidence=0.0,
        raw_json={
            "intent": "unknown",
            "confidence": 0.0,
            "error": "default_fallback",
        },
    )

    if not OPENAI_API_KEY:
        # Log missing config and return default
        loki.log(
            "error",
            {
                "event_type": "service_error",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "error": "OPENAI_API_KEY not set",
            },
            service_type="intent_service",
            sync_mode="async",
            io="none",
        )
        return default_result

    # ------- Log OUTGOING call (async-style) --------
    loki.log(
        "info",
        {
            "event_type": "service_call",
            "reason": "classify_intent",
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
        },
        service_type="intent_service",
        sync_mode="async",  # external HTTP to OpenAI
        io="out",
    )

    client = get_openai_client()

    system_prompt = """
You are an intent classifier for Blink's food assistant.

Your job is to classify the user's latest message into ONE of these intents:

- "menu"      : user is asking about the menu, dishes, what they can order.
- "order"     : user is expressing desire to place an order (even if vague).
- "greeting"  : user is just greeting ("hi", "hello", "hey") with no clear food intent.
- "smalltalk" : user is chatting, asking about you, or saying things unrelated to ordering.
- "unknown"   : cannot confidently match any of the above.

Return ONLY a JSON object with keys:
- "intent"     : string, one of ["menu","order","greeting","smalltalk","unknown"]
- "confidence" : number between 0.0 and 1.0 indicating classifier confidence.

No extra text, no explanations. Only JSON.
""".strip()

    history = history or []
    history_str = ""
    if history:
        # lightweight representation
        history_str = "Conversation history:\n" + "\n".join(
            f"- {msg}" for msg in history[-5:]
        )

    user_prompt = (
        (history_str + "\n\n" if history_str else "")
        + "User message:\n"
        + text
    )

    try:
        resp = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        content = resp.choices[0].message.content or ""

        # Parse JSON
        try:
            data = json.loads(content)
        except Exception:
            data = {}

        intent = str(data.get("intent", "unknown")).strip().lower()
        confidence_raw = data.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except Exception:
            confidence = 0.0

        if intent not in {"menu", "order", "greeting", "smalltalk", "unknown"}:
            intent = "unknown"

        result = IntentResult(intent=intent, confidence=confidence, raw_json=data)

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ------- Log INCOMING result --------
        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "intent": intent,
                "confidence": confidence,
            },
            service_type="intent_service",
            sync_mode="async",
            io="in",
        )

        return result

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        loki.log(
            "error",
            {
                "event_type": "service_error",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "error": str(e),
            },
            service_type="intent_service",
            sync_mode="async",
            io="none",
        )
        return default_result
