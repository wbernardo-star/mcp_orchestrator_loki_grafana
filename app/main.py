# app/main.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_loki import loki
from .menu_service import fetch_menu


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


def detect_route(text: str) -> str:
    """
    Super-simple router:
      - "menu"   → call menu_service
      - default  → generic reply (for now)
    Later you can expand this to call intent_service, order_service, etc.
    """
    t = text.lower().strip()
    menu_keywords = [
        "get the menu",
        "show me the menu",
        "what's on the menu",
        "whats on the menu",
        "read me the menu",
        "can you read me the menu",
        "menu please",
        "i want to see the menu",
    ]
    if any(kw in t for kw in menu_keywords):
        return "menu"
    return "fallback"


def extract_menu_text(menu_payload: Dict) -> str:
    """
    Extract a human-readable menu text from the menu_service (n8n) response.

    Supports shapes like:
      { "output": "Here is the menu ..." }      # AI agent style
      { "menu": "..." }                         # explicit key
      { "categories": [ { name, items[...] } ]} # structured menu
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
            if lines:
                return "Here is the menu:\n" + "\n".join(lines)
        except Exception:
            # if parsing fails, just fall through to ""
            pass

    return ""


# ----------------- FastAPI app -----------------


app = FastAPI(title="MCP Orchestrator – Thin Sync (Menu microservice first)")


@app.get("/health")
def health_check():
    loki.log(
        level="info",
        payload={"event_type": "health"},
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

    # 2) Decide which route / microservice to call
    route = detect_route(req.text)
    state.last_route = route

    # 3) Log INPUT at orchestrator level
    loki.log(
        level="info",
        payload={
            "event_type": "input",
            "user": req.user_id,
            "channel": req.channel,
            "session_id": session_id,
            "turn": state.turn_count,
            "route": route,
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

        # 4) Log OUTPUT at orchestrator level
        loki.log(
            level="info",
            payload={
                "event_type": "output",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "route": route,
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

        # 5) Log ERROR at orchestrator level
        loki.log(
            level="error",
            payload={
                "event_type": "error",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "route": route,
                "error": str(e),
            },
            service_type="orchestrator",
            sync_mode="sync",
            io="none",
        )
        raise HTTPException(status_code=500, detail="Internal error in orchestrator")
