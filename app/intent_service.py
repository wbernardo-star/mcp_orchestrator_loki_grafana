#Grafana intent_service.py Fix

# app/intent_service.py

import os
import time
from typing import Optional

from pydantic import BaseModel
from openai import OpenAI

from .logging_loki import loki


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
else:
    print("[intent_service] OPENAI_API_KEY not set – intent classification will be stubbed.")


class IntentResult(BaseModel):
    intent: str
    confidence: float
    raw_reasoning: str


def _stub_intent(text: str) -> IntentResult:
    """
    Fallback intent classifier when no OpenAI key is configured.
    Keeps behaviour similar to what you had before.
    """
    t = text.lower()
    if any(k in t for k in ["menu", "get the menu", "read the menu"]):
        return IntentResult(intent="menu", confidence=0.8, raw_reasoning="keyword match: menu")
    if any(k in t for k in ["order", "buy", "checkout"]):
        return IntentResult(intent="order", confidence=0.7, raw_reasoning="keyword match: order")
    if any(k in t for k in ["hi", "hello", "good morning", "good evening"]):
        return IntentResult(intent="greeting", confidence=0.6, raw_reasoning="keyword match: greeting")
    return IntentResult(intent="smalltalk", confidence=0.5, raw_reasoning="fallback: smalltalk")


def classify_intent(
    text: str,
    user_id: str,
    channel: str,
    session_id: str,
    history: Optional[list] = None,
) -> IntentResult:
    """
    Classify the user's intent using OpenAI (or stub if no key).
    This is treated as an ASYNC-style microservice in logging terms.
    """

    # --- LOG: service_call (async / out) ---
    start = time.perf_counter()
    loki.log(
        "info",
        {
            "event_type": "service_call",
            "reason": "classify_intent",
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
            "text": text,
        },
        service_type="intent_service",
        sync_mode="async",
        io="out",
    )

    # If no OpenAI key, use the stub and still log service_return
    if client is None:
        result = _stub_intent(text)
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "intent": result.intent,
                "confidence": result.confidence,
                "reason": result.raw_reasoning,
            },
            service_type="intent_service",
            sync_mode="async",
            io="in",
        )
        return result

    try:
        # --------- OpenAI call (adjust if you use a different endpoint) ----------
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an intent classifier for a food-ordering assistant.\n"
                    "Classify the user's message into one of: menu, order, greeting, smalltalk.\n"
                    "Return a short JSON object: {\"intent\": \"...\", \"confidence\": 0.xx, \"reason\": \"...\"}."
                ),
            },
            {"role": "user", "content": text},
        ]

        # Using the chat.completions endpoint for simplicity; change if you use Responses API.
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
        )
        content = completion.choices[0].message.content

        # Very small/loose JSON parsing – you can harden this if needed
        import json

        parsed = {}
        try:
            parsed = json.loads(content)
        except Exception:
            # if model wrapped JSON in backticks or text, try to strip
            try:
                cleaned = content.strip().strip("`").replace("json", "", 1)
                parsed = json.loads(cleaned)
            except Exception:
                parsed = {}

        intent = parsed.get("intent", "unknown")
        confidence = float(parsed.get("confidence", 0.5))
        reason = parsed.get("reason", content)

        result = IntentResult(intent=intent, confidence=confidence, raw_reasoning=reason)

        # --- LOG: service_return (async / in) ---
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "intent": result.intent,
                "confidence": result.confidence,
                "reason": result.raw_reasoning,
            },
            service_type="intent_service",
            sync_mode="async",
            io="in",
        )

        return result

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # --- LOG: service_error ---
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

        # Fall back to stubbed intent on error so the orchestrator can still respond
        return _stub_intent(text)
