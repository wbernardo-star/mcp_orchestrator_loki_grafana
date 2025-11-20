# app/menu_service.py

import os
import time
from typing import Dict, Any

import requests

from .logging_loki import loki


MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL")


def fetch_menu(user_id: str, channel: str, session_id: str) -> Dict[str, Any]:
    """
    Fetch the restaurant menu from an external service (e.g. n8n webhook, REST API).

    Expected to return a JSON like:
    {
      "restaurant": "Blink's Pizza",
      "categories": [
        {
          "name": "Pizza",
          "items": [
            {"name": "Pepperoni", "price": 12.99},
            {"name": "Margherita", "price": 11.49}
          ]
        },
        ...
      ]
    }

    We treat this as an ASYNC-style service in logging:
      sync_mode = "async"
      io       = "out" (call) / "in" (response)
    """

    if not MENU_SERVICE_URL:
        # Menu is not configured; log and return empty.
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
        return {"restaurant": None, "categories": []}

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
        resp = requests.get(MENU_SERVICE_URL, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ---- INCOMING RESPONSE LOG (async IN) ----
        categories = data.get("categories", [])
        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "menu_category_count": len(categories),
            },
            service_type="menu_service",
            sync_mode="async",
            io="in",
        )

        return data

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
        # Fail gracefully with empty menu
        return {"restaurant": None, "categories": []}
