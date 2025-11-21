#app/main.py New


# app/main.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_loki import loki
from .menu_service import fetch_menu
from .intent_service import classify_intent  # LLM-based intent classifier


# ----------------- Pydantic models -----------------


class OrchestrateRequest(BaseModel):
    text: str
    user_id: str
    channel: str = "web"
    session_id: Optional[str] = None


class OrchestrateResponse(BaseModel):
    decision: str
    reply_text: str
    session_id: str
    route: str  # which microservice / path was used (e.g. "menu", "fallback")


# ----------------- Simple in-memory session state -----------------


class SessionState(BaseModel):
    """Very thin session state – orchestration only, no business rules."""
    turn_count: int = 0
    last_active_at: Optional[datetime] = None
    last_route: Optional[str] = None


SESSION_STORE: Dict[str, SessionState] = {}


def get_session(session_id: str) -> SessionState:
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = SessionState()
    return SESSION_STORE[session_id]


# ----------------- Helpers -----------------
def extract_menu_text(menu_payload: Dict) -> str:
    """
    Extract a human-readable menu text from the menu_service (n8n) response.

    Supports shapes like:
      { "output": "Here is the menu ..." }      # AI agent style
      { "menu": "..." }                         # explicit key
      { "categories": [ { name, items[...] } ]} # structured menu

    If structured parsing fails, we fall back to:
      - the longest string anywhere in the JSON payload.
    """

    if not isinstance(menu_payload, dict):
        return ""

    # 1) AI / Respond-to-webhook style
    if isinstance(menu_payload.get("output"), str):
        return menu_payload["output"].strip()

    # 2) Alternate explicit key
    if isinstance(menu_payload.get("menu"), str):
        return menu_payload["menu"].strip()

    # 3) Structured categories → build a simple text
    if "categories" in menu_payload:
        try:
            cats = menu_payload["categories"]
            lines = []
            for c in cats:
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

            # If we got something more meaningful than a single default "Category"
            if lines and not (len(lines) == 1 and lines[0] == "Category"):
                return "Here is the menu:\n" + "\n ".join(lines)
        except Exception:
            # fall through to generic fallback
            pass

    # 4) Generic fallback: find the longest string anywhere in the JSON structure
    def _walk_and_collect_strings(node):
        strings = []
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                strings.extend(_walk_and_collect_strings(v))
        elif isinstance(node, list):
            for v in node:
                strings.extend(_walk_and_collect_strings(v))
        return strings

    all_strings = _walk_and_collect_strings(menu_payload)
    if all_strings:
        # Pick the longest string – typically the big menu paragraph
        longest = max(all_strings, key=len).strip()
        if longest:
            return longest

    # Nothing useful found
    return ""



# ----------------- FastAPI app -----------------


app = FastAPI(title="MCP Orchestrator – Thin Sync (Intent + Menu microservice)")


@app.get("/health")
def health_check():
    # NOTE: using positional args for LokiLogger.log(level, message, **labels)
    loki.log(
        "info",
        {"event_type": "health"},
        service_type="orchestrator",
        sync_mode="sync",
        io="none",
    )
    return {"status": "ok", "service": "mcp_orchestrator_sync_thin"}


@app.post("/orchestrate", response_model=OrchestrateResponse)
def orchestrate(req: OrchestrateRequest):
    start = time.perf_counter()

    # 1) Resolve session
    session_id = req.session_id or f"{req.user_id}:{req.channel}"
    state = get_session(session_id)
    state.turn_count += 1
    state.last_active_at = datetime.now(timezone.utc)

    # 2) Safe defaults for intent in case intent_service fails
    intent: str = "unknown"
    intent_confidence: Optional[float] = None

    # 3) Call internal LLM intent service (instead of keyword detect_route)
    try:
        intent_result = classify_intent(
            text=req.text,
            user_id=req.user_id,
            channel=req.channel,
            session_id=session_id,
            history=None,  # you can pass short history later
        )
        intent = intent_result.intent
        intent_confidence = intent_result.confidence
    except Exception as e:
        # Log intent-service failure but continue with fallback logic
        loki.log(
            "error",
            {
                "event_type": "intent_error",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "error": str(e),
            },
            service_type="intent_service",
            sync_mode="async",
            io="none",
        )
        # keep intent="unknown" and intent_confidence=None

    # 4) Map intent → route
    if intent == "menu":
        route = "menu"
    else:
        route = "fallback"

    state.last_route = route

    # 5) Log INPUT at orchestrator level (including intent)
    loki.log(
        "info",
        {
            "event_type": "input",
            "user": req.user_id,
            "channel": req.channel,
            "session_id": session_id,
            "turn": state.turn_count,
            "route": route,
            "intent": intent,
            "intent_confidence": intent_confidence,
            "text": req.text,
        },
        service_type="orchestrator",
        sync_mode="sync",
        io="in",
    )

    try:
        # ------------------- ROUTING -------------------

        if route == "menu":
            # Call the menu microservice (n8n webhook, etc.)
            menu_payload = fetch_menu(
                user_id=req.user_id,
                channel=req.channel,
                session_id=session_id,
            )
            menu_text = extract_menu_text(menu_payload)

            if menu_text:
                reply_text = menu_text
            else:
                reply_text = (
                    "I tried to fetch the menu but didn't receive any usable data. "
                    "Please try again in a moment."
                )

        else:
            # For now, simple fallback: echo-style
            reply_text = (
                "I can show you the restaurant menu. "
                "Try saying something like: 'Get the menu'.\n\n"
                f"(You said: {req.text})"
            )

        # ------------------- END ROUTING -------------------

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # 6) Log OUTPUT at orchestrator level
        loki.log(
            "info",
            {
                "event_type": "output",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "route": route,
                "intent": intent,
                "intent_confidence": intent_confidence,
                "message": "request_end",
            },
            service_type="orchestrator",
            sync_mode="sync",
            io="out",
        )

        return OrchestrateResponse(
            decision="reply",
            reply_text=reply_text,
            session_id=session_id,
            route=route,
        )

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # 7) Log ERROR at orchestrator level
        loki.log(
            "error",
            {
                "event_type": "error",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "route": route,
                "intent": intent,
                "intent_confidence": intent_confidence,
                "error": str(e),
            },
            service_type="orchestrator",
            sync_mode="sync",
            io="none",
        )
        raise HTTPException(status_code=500, detail="Internal error in orchestrator")
