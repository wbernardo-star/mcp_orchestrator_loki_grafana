#Flow menu_service.py Fix

# app/menu_service.py

import os
import time
from typing import Dict, Any

import requests

from .logging_loki import loki


MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL")


def fetch_menu(user_id: str, channel: str, session_id: str) -> Dict[str, Any]:
    """
    Fetch the restaurant menu from an external service (n8n webhook).

    Normalizes n8n's response shape, e.g.:

    [
      {
        "output": { "text": "Pepperoni - Pizza - 100\n..." }
      }
    ]

    into:

    {
      "output": "Pepperoni - Pizza - 100\n..."
    }
    """

    if not MENU_SERVICE_URL:
        loki.log(
            "warning",
            {
                "event_type": "service_missing_config",
                "detail": "MENU_SERVICE_URL not set",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
            },
            service_type="menu_service",
            sync_mode="async",
            io="none",
        )
        return {}

    start = time.perf_counter()

    # ---- OUTGOING CALL LOG (async OUT) ----
    loki.log(
        "info",
        {
            "event_type": "service_call",
            "reason": "get_menu",
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
        },
        service_type="menu_service",
        sync_mode="async",
        io="out",
    )

    try:
        resp = requests.get(MENU_SERVICE_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        # --- NORMALIZATION STEP ---

        # If it's a list, take the first element
        if isinstance(data, list) and data:
            first = data[0]
        else:
            first = data

        # Expect dict with "output"
        if isinstance(first, dict):
            out = first.get("output")
        else:
            out = None

        # If output is an object with "text", flatten it
        if isinstance(out, dict) and isinstance(out.get("text"), str):
            normalized = {"output": out["text"]}
        # If output is already a string, just pass it through
        elif isinstance(out, str):
            normalized = {"output": out}
        else:
            # Unknown shape – log and treat as empty
            normalized = {}

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ---- INCOMING RESPONSE LOG (async IN) ----
        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "raw_shape": type(data).__name__,
            },
            service_type="menu_service",
            sync_mode="async",
            io="in",
        )

        return normalized

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
            service_type="menu_service",
            sync_mode="async",
            io="none",
        )
        return {}
