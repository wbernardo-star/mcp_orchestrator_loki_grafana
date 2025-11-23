#FLOWSERVICE app/main.py

# app/main.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_loki import loki
from .intent_service import classify_intent
from .flow_service import run_flow   # ← NEW: flow microservice orchestrator


# ------------------------------------------------------
#  Pydantic Models (Orchestrator Input/Output)
# ------------------------------------------------------

class OrchestrateRequest(BaseModel):
    text: str
    user_id: str
    channel: str = "web"
    session_id: Optional[str] = None


class OrchestrateResponse(BaseModel):
    decision: str
    reply_text: str
    session_id: str
    route: str
    intent: str
    intent_confidence: float


# ------------------------------------------------------
#  Session State (thin)
# ------------------------------------------------------

class SessionState(BaseModel):
    """Minimal session state — the flow microservice will hold domain logic."""
    turn_count: int = 0
    last_active_at: Optional[datetime] = None
    last_route: Optional[str] = None


SESSION_STORE: Dict[str, SessionState] = {}


def get_session(session_id: str) -> SessionState:
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = SessionState()
    return SESSION_STORE[session_id]


# ------------------------------------------------------
#  FastAPI App
# ------------------------------------------------------

app = FastAPI(title="MCP Orchestrator – Ultra Thin (Intent + Flow Service)")


@app.get("/health")
def health_check():
    loki.log(
        "info",
        {"event_type": "health"},
        service_type="orchestrator",
        sync_mode="sync",
        io="none",
    )
    return {"status": "ok", "service": "mcp_orchestrator_thin"}


# ------------------------------------------------------
#  MAIN ORCHESTRATION ENDPOINT
# ------------------------------------------------------

@app.post("/orchestrate", response_model=OrchestrateResponse)
def orchestrate(req: OrchestrateRequest):

    start = time.perf_counter()

    # ------------------------------
    #  SESSION MANAGEMENT
    # ------------------------------
    session_id = req.session_id or f"{req.user_id}:{req.channel}"
    state = get_session(session_id)
    state.turn_count += 1
    state.last_active_at = datetime.now(timezone.utc)

    # ------------------------------
    #  INTENT CLASSIFICATION (LLM)
    # ------------------------------
    intent_result = classify_intent(
        text=req.text,
        user_id=req.user_id,
        channel=req.channel,
        session_id=session_id,
        history=None,  # optional future
    )
    intent = intent_result.intent
    confidence = intent_result.confidence

    # ------------------------------
    #  LOG INPUT EVENT
    # ------------------------------
    loki.log(
        "info",
        {
            "event_type": "input",
            "user": req.user_id,
            "channel": req.channel,
            "session_id": session_id,
            "turn": state.turn_count,
            "intent": intent,
            "intent_confidence": confidence,
            "text": req.text,
        },
        service_type="orchestrator",
        sync_mode="sync",
        io="in",
    )

    # ------------------------------
    #  FLOW SERVICE (Domain Logic)
    # ------------------------------
    try:
        flow_result = run_flow(
            intent=intent,
            text=req.text,
            user_id=req.user_id,
            channel=req.channel,
            session_id=session_id,
        )

        reply_text = flow_result.reply_text
        route = flow_result.route

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ------------------------------
        #  LOG OUTPUT EVENT
        # ------------------------------
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
                "intent_confidence": confidence,
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
            intent=intent,
            intent_confidence=confidence,
        )

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ------------------------------
        #  LOG ERROR EVENT
        # ------------------------------
        loki.log(
            "error",
            {
                "event_type": "error",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "intent": intent,
                "intent_confidence": confidence,
                "error": str(e),
            },
            service_type="orchestrator",
            sync_mode="sync",
            io="none",
        )

        raise HTTPException(status_code=500, detail="Internal error in orchestrator")
