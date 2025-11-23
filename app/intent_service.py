#FLOWSERVICE app/intent_service.py

# app/intent_service.py

from __future__ import annotations

import os
import time
from typing import Optional
from pydantic import BaseModel

from openai import OpenAI
from .logging_loki import loki


# ------------------------------------------------------
# Pydantic result model
# ------------------------------------------------------

class IntentResult(BaseModel):
    intent: str
    confidence: float
    raw_text: str


# ------------------------------------------------------
# OpenAI client (v2 SDK)
# ------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = os.getenv("INTENT_MODEL", "gpt-4o-mini")


# ------------------------------------------------------
# Intent classifier
# ------------------------------------------------------

def classify_intent(
    text: str,
    user_id: str,
    channel: str,
    session_id: str,
    history: Optional[list] = None,
) -> IntentResult:
    """
    Classification rules:
         menu
         order
         greeting
         smalltalk
         unknown

    This service is synchronous (sync_mode=sync).
    """

    start = time.perf_counter()

    # ---- log START ----
    loki.log(
        "info",
        {
            "event_type": "intent_call",
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
            "text": text,
        },
        service_type="intent_service",
        sync_mode="sync",
        io="out",
    )

    prompt = f"""
Classify the user's intent into EXACTLY one of the following labels:

- menu        (asking for the menu)
- order       (wants to order food)
- greeting    (hello, hi, hey)
- smalltalk   (conversation that is neither menu nor order)
- unknown     (does not match anything)

Return ONLY JSON in this format:
{{
  "intent": "...",
  "confidence": 0.0
}}

User message:
{text}
"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=50,
        )

        raw = response.choices[0].message.content.strip()

        # Safely parse JSON
        import json
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"intent": "unknown", "confidence": 0.1}

        intent = parsed.get("intent", "unknown").lower()
        confidence = float(parsed.get("confidence", 0.1))

        # Validate
        allowed = {"menu", "order", "greeting", "smalltalk", "unknown"}
        if intent not in allowed:
            intent = "unknown"

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ---- log RETURN ----
        loki.log(
            "info",
            {
                "event_type": "intent_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "intent": intent,
                "confidence": confidence,
                "latency_ms": latency_ms,
            },
            service_type="intent_service",
            sync_mode="sync",
            io="in",
        )

        return IntentResult(
            intent=intent,
            confidence=confidence,
            raw_text=text,
        )

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        loki.log(
            "error",
            {
                "event_type": "intent_error",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "error": str(e),
                "latency_ms": latency_ms,
            },
            service_type="intent_service",
            sync_mode="sync",
            io="none",
        )

        return IntentResult(
            intent="unknown",
            confidence=0.0,
            raw_text=text,
        )
