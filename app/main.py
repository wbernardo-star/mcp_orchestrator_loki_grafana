#main.py extract_menu fix

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_loki import loki
from .menu_service import fetch_menu
from .intent_service import classify_intent  # LLM-based intent service


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


def extract_menu_text(menu_payload: Any) -> str:
    """
    Extract a human-readable menu text from the menu_service (n8n) response.

    Supports shapes like:
      { "output": "Here is the menu ..." }            # AI agent style
      { "menu": "..." }                               # explicit key
      { "categories": [ { name, items[...] } ] }      # structured menu
      [ { "output": "..." } ]                         # list-wrapped
      or, as a last resort, any long string value in the JSON.
    """

    # If n8n wrapped it in a list, unwrap the first element
    if isinstance(menu_payload, list) and menu_payload:
        menu_payload = menu_payload[0]

    # If it's just a raw string, return it
    if isinstance(menu_payload, str):
        return menu_payload.strip()

    if not isinstance(menu_payload, dict):
        return ""

    # 1) AI / Respond-to-webhook style
    out = menu_payload.get("output")
    if isinstance(out, str) and out.strip():
        return out.strip()

    # 2) Alternate explicit key
    out = menu_payload.get("menu")
    if isinstance(out, str) and out.strip():
        return out.strip()

    # 3) Any “long” string value anywhere in the dict
    for v in menu_payload.values():
        if isinstance(v, str) and len(v.strip()) > 40:
            return v.strip()

    # 4) Structured categories → build a readable text
    if "categories" in menu_payload and isinstance(menu_payload["categories"], list):
        lines = []
        for cat in menu_payload["categories"]:
            if not isinstance(cat, dict):
                continue

            cname = str(cat.get("name", "Category"))
            items = (
                cat.get("items")
                or cat.get("menu_items")
                or []
            )

            item_names = []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        nm = it.get("name") or it.get("title") or ""
                        if not nm:
                            continue
                        price = it.get("price")
                        if price is not None:
                            item_names.append(f"{nm} ({price})")
                        else:
                            item_names.append(str(nm))
                    elif isinstance(it, str):
                        item_names.append(it)

            if item_names:
                lines.append(f"{cname}: " + ", ".join(item_names))
            else:
                # At least show the category name, but not *only* that
                lines.append(cname)

        if lines:
            return "Here is the menu:\n" + "\n".join(lines)

    # 5) Fallback – stringify the JSON so the user still sees something
    return str(menu_payload)


# ----------------- FastAPI app -----------------


app = FastAPI(title="MCP Orchestrator – Thin Sync (Intent + Menu microservice)")


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

    # 2) Call internal LLM intent service
    intent_result = classify_intent(
        text=req.text,
        user_id=req.user_id,
        channel=req.channel,
        session_id=session_id,
        history=None,
    )
    intent = intent_result.intent  # "menu", "order", "greeting", "smalltalk", "unknown"

    # 3) Map intent → route
    if intent == "menu":
        route = "menu"
    else:
        route = "fallback"

    state.last_route = route

    # 4) Log INPUT at orchestrator level (including intent)
    loki.log(
        level="info",
        payload={
            "event_type": "input",
            "user": req.user_id,
            "channel": req.channel,
            "session_id": session_id,
            "turn": state.turn_count,
            "route": route,
            "intent": intent,
            "intent_confidence": intent_result.confidence,
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

        # 5) Log OUTPUT at orchestrator level
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
                "intent": intent,
                "intent_confidence": intent_result.confidence,
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

        # 6) Log ERROR at orchestrator level
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
                "intent": intent,
                "intent_confidence": intent_result.confidence,
                "error": str(e),
            },
            service_type="orchestrator",
            sync_mode="sync",
            io="none",
        )
        raise HTTPException(status_code=500, detail="Internal error in orchestrator")
