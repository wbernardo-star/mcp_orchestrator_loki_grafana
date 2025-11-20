#async main.py

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logging_loki import loki
from .menu_service import fetch_menu  # <-- NEW IMPORT


# ----------------- Models -----------------


class OrchestrateRequest(BaseModel):
    text: str
    user_id: str
    channel: str = "web"
    session_id: Optional[str] = None


class OrchestrateResponse(BaseModel):
    decision: str
    reply_text: str
    flow: Optional[str] = None
    step: Optional[str] = None
    session_id: str


# ----------------- Simple in-memory Session Context -----------------


class SessionState(BaseModel):
    flow: Optional[str] = None
    step: Optional[str] = None
    scratchpad: Dict[str, str] = {}
    turn_count: int = 0
    last_active_at: Optional[datetime] = None


SESSION_STORE: Dict[str, SessionState] = {}


def get_session(session_id: str) -> SessionState:
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = SessionState()
    return SESSION_STORE[session_id]


def reset_session(session_id: str) -> None:
    SESSION_STORE[session_id] = SessionState()


# ----------------- Food-ordering core -----------------


def handle_food_flow(
    text: str,
    state: SessionState,
    user_id: str,
    channel: str,
    session_id: str,
) -> Tuple[str, bool]:
    """
    Returns (reply_text, reset_after_reply_flag).
    Implements your hard-coded food ordering flow, extended to call menu_service.
    """
    text_lower = text.lower().strip()
    reset_after_reply = False

    # 1) Start the food ordering flow
    if state.flow is None and any(
        kw in text_lower for kw in ["order", "food", "pizza", "burger", "menu", "cravings"]
    ):
        state.flow = "food_order"
        state.step = "ask_category"
        state.scratchpad["awaiting_category"] = "1"

        # 🔹 Call the Menu Service (async-style service)
        menu_json = fetch_menu(user_id=user_id, channel=channel, session_id=session_id)
        state.scratchpad["menu"] = menu_json  # stored as JSON; can be used later

        reply_text = (
            "Nice, let's order some food!\n"
            "What type of food would you like? (example. pizza, burger, salad, chicken, ramen)"
        )

    # 2) Ask category (pizza, burger, etc.)
    elif state.flow == "food_order" and state.step == "ask_category":
        state.scratchpad["category"] = text
        state.step = "collect_items"
        state.scratchpad.pop("awaiting_category", None)
        state.scratchpad["awaiting_items"] = "1"
        reply_text = (
            f"Great, {text}!\n"
            "What food items would you like to order? Example: '1 large pepperoni, 1 garlic bread'."
        )

    # 3) Collect food items
    elif state.flow == "food_order" and state.step == "collect_items":
        state.scratchpad["items"] = text
        state.step = "ask_address"
        state.scratchpad.pop("awaiting_items", None)
        state.scratchpad["awaiting_address"] = "1"
        reply_text = "Got it!\nNext, what's the delivery address?"

    # 4) Ask for address
    elif state.flow == "food_order" and state.step == "ask_address":
        state.scratchpad["address"] = text
        state.step = "ask_phone"
        state.scratchpad.pop("awaiting_address", None)
        state.scratchpad["awaiting_phone"] = "1"
        reply_text = "Great — and what phone number should the driver call?"

    # 5) Ask for phone number
    elif state.flow == "food_order" and state.step == "ask_phone":
        state.scratchpad["phone"] = text
        state.step = "confirm_order"
        state.scratchpad.pop("awaiting_phone", None)
        state.scratchpad["awaiting_confirmation"] = "1"

        category = state.scratchpad.get("category", "food")
        items = state.scratchpad.get("items", "")
        address = state.scratchpad.get("address", "")
        phone = state.scratchpad.get("phone", "")

        reply_text = (
            "Here’s your full order summary:\n"
            f"- Category: {category}\n"
            f"- Items: {items}\n"
            f"- Address: {address}\n"
            f"- Phone: {phone}\n\n"
            "Would you like to place this order? Please say Yes to confirm or No to cancel."
        )

    # 6) Final confirmation
    elif state.flow == "food_order" and state.step == "confirm_order":
        if "yes" in text_lower:
            category = state.scratchpad.get("category")
            items = state.scratchpad.get("items")
            address = state.scratchpad.get("address")
            phone = state.scratchpad.get("phone")

            reply_text = (
                "Your food order has been placed!\n"
                f"- Category: {category}\n"
                f"- Items: {items}\n"
                f"- Address: {address}\n"
                f"- Phone: {phone}\n\n"
                "Thanks for ordering!"
            )
            reset_after_reply = True

        elif "no" in text_lower:
            reply_text = (
                "Okay, I've canceled the order. If you want to try again, "
                "just say you want to order food."
            )
            reset_after_reply = True
        else:
            reply_text = "Please answer with 'yes' or 'no'."

    # Fallback outside the food flow
    else:
        if any(g in text_lower for g in ["hello", "hi", "hey"]):
            reply_text = "Hello! I can help you order food. Just say you want to order."
        else:
            reply_text = f"Echo from orchestrator: {text}"

    return reply_text, reset_after_reply


# ----------------- FastAPI app -----------------


app = FastAPI(title="MCP Orchestrator – Sync + Loki + Menu Service")


@app.get("/health")
def health_check():
    loki.log(
        "info",
        {"event_type": "health"},
        service_type="orchestrator",
        sync_mode="sync",
        io="none",
    )
    return {"status": "ok", "service": "mcp_orchestrator_sync"}


@app.post("/orchestrate", response_model=OrchestrateResponse)
def orchestrate(req: OrchestrateRequest):
    start = time.perf_counter()

    session_id = req.session_id or f"{req.user_id}:{req.channel}"
    state = get_session(session_id)
    state.turn_count += 1
    state.last_active_at = datetime.now(timezone.utc)

    # ---- INPUT log (sync IN) ----
    loki.log(
        "info",
        {
            "event_type": "input",
            "user": req.user_id,
            "channel": req.channel,
            "session_id": session_id,
            "turn": state.turn_count,
            "text": req.text,
        },
        flow=state.flow or "none",
        step=state.step or "none",
        service_type="orchestrator",
        sync_mode="sync",
        io="in",
    )

    try:
        reply_text, reset_after = handle_food_flow(
            text=req.text,
            state=state,
            user_id=req.user_id,
            channel=req.channel,
            session_id=session_id,
        )
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        # ---- OUTPUT log (sync OUT) ----
        loki.log(
            "info",
            {
                "event_type": "output",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "message": "request_end",
            },
            flow=state.flow or "none",
            step=state.step or "none",
            service_type="orchestrator",
            sync_mode="sync",
            io="out",
        )

        flow_name = state.flow
        step_name = state.step

        if reset_after:
            loki.log(
                "info",
                {
                    "event_type": "session_reset",
                    "user": req.user_id,
                    "channel": req.channel,
                    "session_id": session_id,
                    "latency_ms": latency_ms,
                    "reason": "order_complete_or_cancel",
                },
                flow=state.flow or "none",
                step=state.step or "none",
                service_type="orchestrator",
                sync_mode="sync",
                io="none",
            )
            reset_session(session_id)
            flow_name = None
            step_name = None

        return OrchestrateResponse(
            decision="reply",
            reply_text=reply_text,
            flow=flow_name,
            step=step_name,
            session_id=session_id,
        )

    except Exception as e:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)

        loki.log(
            "error",
            {
                "event_type": "error",
                "user": req.user_id,
                "channel": req.channel,
                "session_id": session_id,
                "turn": state.turn_count,
                "latency_ms": latency_ms,
                "error": str(e),
            },
            flow=state.flow or "none",
            step=state.step or "none",
            service_type="orchestrator",
            sync_mode="sync",
            io="none",
        )
        raise HTTPException(status_code=500, detail="Internal error in orchestrator")
