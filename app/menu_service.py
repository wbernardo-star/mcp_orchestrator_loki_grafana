# app/menu_service.py NEW

import os
import time
from typing import Dict, Any

import requests

from .logging_loki import loki


# n8n webhook / external menu service URL
MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL")


def fetch_menu(user_id: str, channel: str, session_id: str) -> Dict[str, Any]:
    """
    Fetch the restaurant menu from an external service (e.g. n8n webhook, REST API).

    The external service might return:
      - A dict, e.g. { "output": "...", "categories": [...] }
      - OR a list, e.g. [ { "name": "Pizza", "items": [...] }, ... ]

    We treat this as an ASYNC-style service in logging:
      sync_mode = "async"
      io       = "out" (call) / "in" (response)
    """

    if not MENU_SERVICE_URL:
        # Menu is not configured; log and return empty menu
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
        # n8n webhook is POST-based
        payload = {
            "action": "get_menu",
            "user_id": user_id,
            "channel": channel,
            "session_id": session_id,
        }

        resp = requests.post(
            MENU_SERVICE_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ---- INCOMING RESPONSE LOG (async IN) ----
        # Robust handling for dict OR list
        if isinstance(data, dict):
            categories_field = data.get("categories", [])
            if isinstance(categories_field, list):
                category_count = len(categories_field)
            else:
                category_count = 0
        elif isinstance(data, list):
            # Bare list → treat it as categories
            category_count = len(data)
        else:
            category_count = 0

        loki.log(
            "info",
            {
                "event_type": "service_return",
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "latency_ms": latency_ms,
                "menu_category_count": category_count,
            },
            service_type="menu_service",
            sync_mode="async",
            io="in",
        )

        # Always return a dict so extract_menu_text can handle it
        if isinstance(data, list):
            return {"categories": data}
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
