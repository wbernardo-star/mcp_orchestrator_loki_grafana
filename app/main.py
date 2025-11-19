
from __future__ import annotations

import logging
import time
from typing import Dict, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, Histogram, generate_latest

from .logging_loki import loki

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mcp.main")

app = FastAPI(title="MCP Orchestrator – Sync + Full Food Flow + Observability", version="1.0.0")

# Metrics
REQ = Counter("mcp_requests_total", "Total MCP orchestrator requests")
LAT = Histogram("mcp_request_latency_seconds", "MCP orchestrator latency in seconds")

# Service catalog (metadata only)
SERVICE_CATALOG: Dict[str, Dict[str, Any]] = {
    "whisper_stt": {
        "mode": "async",
        "protocol": "http",
        "critical": True,
        "description": "Speech-to-Text (listening client / Whisper).",
    },
    "elevenlabs_tts": {
        "mode": "sync",
        "protocol": "http",
        "critical": False,
        "description": "Text-to-Speech output (ElevenLabs).",
    },
    "n8n_intent_validator": {
        "mode": "sync",
        "protocol": "http",
        "critical": True,
        "description": "External intent / menu validation via n8n webhook.",
    },
}

# In-memory session store:
#   { session_id: { flow, step, scratchpad, flags, turn_count } }
SESSION_STORE: Dict[str, Dict[str, Any]] = {}


def get_or_create_session_state(session_id: str) -> Dict[str, Any]:
    if session_id not in SESSION_STORE:
        SESSION_STORE[session_id] = {
            "flow": None,
            "step": None,
            "scratchpad": {},
            "flags": {},
            "turn_count": 0,
        }
    return SESSION_STORE[session_id]


def reset_session_state(session_id: str) -> None:
    SESSION_STORE[session_id] = {
        "flow": None,
        "step": None,
        "scratchpad": {},
        "flags": {},
        "turn_count": 0,
    }


@app.get("/health")
def health():
    logger.info("health_check")
    loki.log("info", "health_check", event="health")
    return {"status": "ok", "block": "mcp_orchestrator_sync_full_flow"}


@app.get("/services")
def services():
    logger.info("services_listed size=%s", len(SERVICE_CATALOG))
    return JSONResponse(content=SERVICE_CATALOG)


@app.get("/metrics")
def metrics():
    return Response(
        generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post("/orchestrate")
def orchestrate(body: Dict[str, Any]):
    """
    Synchronous orchestration endpoint with:
      - multi-session state
      - full food-order flow
      - Loki logging
      - Prometheus metrics

    Expected JSON:
    {
      "text": "I want to order food",
      "user_id": "user-123",
      "channel": "web",
      "session_id": "optional-session-id"
    }
    """
    start = time.time()
    REQ.inc()

    text = (body.get("text") or "").strip()
    user_id = body.get("user_id") or "anonymous"
    channel = body.get("channel") or "unknown"
    session_id = body.get("session_id") or f"{user_id}:{channel}"

    state = get_or_create_session_state(session_id)
    state["turn_count"] += 1

    logger.info(
        "request_start session_id=%s user_id=%s channel=%s text=%s flow=%s step=%s turn=%s",
        session_id,
        user_id,
        channel,
        text,
        state["flow"],
        state["step"],
        state["turn_count"],
    )
    loki.log(
        "info",
        "request_start",
        event="input",
        user=user_id,
        channel=channel,
        session_id=session_id,
        flow=state["flow"],
        step=state["step"],
        turn=state["turn_count"],
    )

    # --------------------------------------------------
    #  FULL STATEFUL FOOD ORDERING FLOW
    # --------------------------------------------------
    text_lower = text.lower()
    reset_after_reply = False

    # 1) Start flow
    if state["flow"] is None and any(
        kw in text_lower for kw in ["order", "food", "pizza", "burger", "menu", "cravings"]
    ):
        state["flow"] = "food_order"
        state["step"] = "ask_category"
        state["flags"]["awaiting_category"] = True

        reply_text = (
            "Nice, let's order some food!\n"
            "What type of food would you like? (e.g., pizza, burger, salad, chicken, ramen)"
        )

    # 2) Ask category
    elif state["flow"] == "food_order" and state["step"] == "ask_category":
        state["scratchpad"]["category"] = text
        state["step"] = "collect_items"
        state["flags"]["awaiting_category"] = False
        state["flags"]["awaiting_items"] = True

        reply_text = (
            f"Great, {text}!\n"
            "What food items would you like to order? "
            "Example: '1 large pepperoni, 1 garlic bread'."
        )

    # 3) Collect items
    elif state["flow"] == "food_order" and state["step"] == "collect_items":
        state["scratchpad"]["items"] = text
        state["step"] = "ask_address"
        state["flags"]["awaiting_items"] = False
        state["flags"]["awaiting_address"] = True

        reply_text = "Got it!\nNext, what's the delivery address?"

    # 4) Ask address
    elif state["flow"] == "food_order" and state["step"] == "ask_address":
        state["scratchpad"]["address"] = text
        state["step"] = "ask_phone"
        state["flags"]["awaiting_address"] = False
        state["flags"]["awaiting_phone"] = True

        reply_text = "Great — and what phone number should the driver call?"

    # 5) Ask phone
    elif state["flow"] == "food_order" and state["step"] == "ask_phone":
        state["scratchpad"]["phone"] = text
        state["step"] = "confirm_order"
        state["flags"]["awaiting_phone"] = False
        state["flags"]["awaiting_confirmation"] = True

        category = state["scratchpad"].get("category", "food")
        items = state["scratchpad"].get("items", "")
        address = state["scratchpad"].get("address", "")
        phone = state["scratchpad"].get("phone", "")

        reply_text = (
            "Here’s your full order summary:\n"
            f"- Category: {category}\n"
            f"- Items: {items}\n"
            f"- Address: {address}\n"
            f"- Phone: {phone}\n\n"
            "Would you like to place this order? Please say Yes to confirm or No to cancel."
        )

    # 6) Confirm
    elif state["flow"] == "food_order" and state["step"] == "confirm_order":
        if "yes" in text_lower:
            state["step"] = "order_placed"
            state["flags"]["awaiting_confirmation"] = False

            category = state["scratchpad"].get("category")
            items = state["scratchpad"].get("items")
            address = state["scratchpad"].get("address")
            phone = state["scratchpad"].get("phone")

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

    # Fallback
    else:
        if any(g in text_lower for g in ["hello", "hi", "hey"]):
            reply_text = f"Hello, {user_id}! (from MCP Orchestrator)"
        else:
            reply_text = f"Echo from orchestrator: {text}"

    # --------------------------------------------------
    #  END OF FLOW LOGIC
    # --------------------------------------------------
    latency = time.time() - start
    LAT.observe(latency)

    logger.info(
        "request_end session_id=%s user_id=%s flow=%s step=%s latency=%.4f turn=%s",
        session_id,
        user_id,
        state.get("flow"),
        state.get("step"),
        latency,
        state.get("turn_count"),
    )
    loki.log(
        "info",
        "request_end",
        event="output",
        user=user_id,
        channel=channel,
        session_id=session_id,
        flow=state.get("flow"),
        step=state.get("step"),
        latency=f"{latency:.4f}",
        turn=state.get("turn_count"),
    )

    if reset_after_reply:
        logger.info("session_reset session_id=%s", session_id)
        loki.log(
            "info",
            "session_reset",
            event="session_reset",
            session_id=session_id,
            user=user_id,
        )
        reset_session_state(session_id)

    resp = {
        "decision": "reply",
        "reply_text": reply_text,
        "memory_snapshot": {
            "session_id": session_id,
            "turn_count": state.get("turn_count", 0),
        },
        "state": {
            "flow": state.get("flow"),
            "step": state.get("step"),
            "scratchpad": state.get("scratchpad", {}),
            "flags": state.get("flags", {}),
        },
        "debug": {
            "latency_seconds": round(latency, 4),
        },
    }
    return JSONResponse(content=resp)
