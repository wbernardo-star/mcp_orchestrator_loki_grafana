#FLOWSERVICE app/menu_service.py

# app/menu_service.py

from __future__ import annotations

import os
import time
from typing import Dict, Any

import requests

from .logging_loki import loki


# ------------------------------------------------------
# Environment
# ------------------------------------------------------

MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL")   # n8n webhook URL
MENU_TIMEOUT = float(os.getenv("MENU_SERVICE_TIMEOUT", "8.0"))


# ------------------------------------------------------
# Menu Service (async microservice)
# ------------------------------------------------------

def fetch_menu(user_id: str, channel: str, session_id: str) -> Dict[str, Any]:
    """
    Calls the external menu microservice (n8n webhook or REST API)
    to retrieve the restaurant menu.

    This is treated as an ASYNC microservice in logging:
       sync_mode = "async"
       io        = "out" (request) / "in" (response)

    Expected response formats:
      {
        "output": "Here is the menu..."
      }
      {
        "menu": "..."
      }
      {
        "categories": [
            { "name": "...", "items": [ { "name": "...", "price": 10.50 }, ... ] }
        ]
      }
    """

    # If no URL is configured → log and return empty
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
        return {"error": "MENU_SERVICE_URL not configured", "categories": []}

    start = time.perf_counter()

    # ------------- LOG OUTGOING REQUEST -------------
    loki.log(
        "info",
        {
            "event_type": "service_call",
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
            "reason": "fetch_menu",
        },
        service_type="menu_service",
        sync_mode="async",
        io="out",
    )

    try:
        # n8n usually expects GET, but can also be POST depending on your workflow
        response = requests.get(MENU_SERVICE_URL, timeout=MENU_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        cat_count = 0
        if isinstance(data, dict) and "categories" in data and isinstance(data["categories"], list):
            cat_count = len(data["categories"])

        # ------------- LOG SUCCESS RESPONSE -------------
        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "menu_category_count": cat_count,
            },
            service_type="menu_service",
            sync_mode="async",
            io="in",
        )

        return data

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ------------- LOG ERROR RESPONSE -------------
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

        return {
            "error": str(e),
            "categories": []
        }
