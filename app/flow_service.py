#FLOWSERVICE app/flow_service.py

# app/flow_service.py

from __future__ import annotations

import time
from typing import Optional
from pydantic import BaseModel

from .logging_loki import loki
from .menu_service import fetch_menu


# ------------------------------------------------------
# FlowServiceResult model
# ------------------------------------------------------

class FlowServiceResult(BaseModel):
    reply_text: str
    route: str   # which microservice or flow handled the request


# ------------------------------------------------------
# Main Flow Service Logic
# ------------------------------------------------------

def run_flow(
    intent: str,
    text: str,
    user_id: str,
    channel: str,
    session_id: str,
) -> FlowServiceResult:
    """
    Flow Service = domain orchestration layer.

    Responsibilities:
      - Route by INTENT (determined by intent_service.py)
      - Call correct microservice(s)
      - Assemble final reply_text
      - Handle business logic OUTSIDE of MCP

    The orchestrator simply calls `run_flow()` and returns the result.
    """

    # Log start of flow handling
    loki.log(
        "info",
        {
            "event_type": "flow_start",
            "intent": intent,
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
        },
        service_type="flow_service",
        sync_mode="sync",
        io="in",
    )

    # ======================================================
    #  INTENT → FLOW ROUTING
    # ======================================================

    # 1. MENU FLOW
    if intent == "menu":
        start = time.perf_counter()

        loki.log(
            "info",
            {
                "event_type": "flow_menu_call",
                "intent": intent,
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "message": "calling menu_service",
            },
            service_type="flow_service",
            sync_mode="async",
            io="out",
        )

        # Call external microservice
        menu_payload = fetch_menu(
            user_id=user_id,
            channel=channel,
            session_id=session_id,
        )

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # Extract human-readable menu
        reply_text = _extract_menu_text(menu_payload)

        # Log return from menu_service
        loki.log(
            "info",
            {
                "event_type": "flow_menu_return",
                "intent": intent,
                "latency_ms": latency_ms,
                "user": user_id,
                "channel": channel,
                "session_id": session_id,
                "payload_received": True,
            },
            service_type="flow_service",
            sync_mode="async",
            io="in",
        )

        if not reply_text:
            reply_text = (
                "I tried to fetch the menu but didn't receive any usable data. "
                "Please try again in a moment."
            )

        return FlowServiceResult(reply_text=reply_text, route="menu")

    # ======================================================
    #  UNKNOWN / DEFAULT FLOW
    # ======================================================

    reply_text = (
        "I can help you by getting the menu. "
        "Try saying something like: 'Get the menu'.\n\n"
        f"(You said: {text})"
    )

    loki.log(
        "info",
        {
            "event_type": "flow_fallback",
            "intent": intent,
            "user": user_id,
            "channel": channel,
            "session_id": session_id,
        },
        service_type="flow_service",
        sync_mode="sync",
        io="none",
    )

    return FlowServiceResult(reply_text=reply_text, route="fallback")


# ------------------------------------------------------
# Helper: Convert menu_payload into readable text
# ------------------------------------------------------

def _extract_menu_text(menu_payload: dict) -> str:
    """Reusable version of menu formatting."""

    if not isinstance(menu_payload, dict):
        return ""

    # AI-style text response
    if isinstance(menu_payload.get("output"), str):
        return menu_payload["output"].strip()

    # Explicit key "menu"
    if isinstance(menu_payload.get("menu"), str):
        return menu_payload["menu"].strip()

    # Structured category list
    if "categories" in menu_payload:
        try:
            lines = []
            for c in menu_payload["categories"]:
                if not isinstance(c, dict):
                    continue
                name = c.get("name", "Category")
                items = c.get("items") or []
                item_names = ", ".join(
                    i.get("name", "") for i in items if isinstance(i, dict)
                )
                if item_names:
                    lines.append(f"{name}: {item_names}")
                else:
                    lines.append(name)

            if lines:
                return "Here is the menu:\n" + "\n".join(lines)

        except Exception:
            return ""

    return ""
