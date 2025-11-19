#Loki Py MS

import os
import time
import json
import requests


class LokiLogger:
    """
    Helper to push structured logs to Grafana Loki.

    Env vars:
      - GRAFANA_LOKI_URL       e.g. https://logs-prod-025.grafana.net/loki/api/v1/push
      - GRAFANA_LOKI_USERNAME  e.g. 1401328  (tenant / user ID)
      - GRAFANA_LOKI_API_TOKEN token with logs:write
      - MCP_APP_LABEL          (optional) app label, default "mcp_orchestrator_sync"

    Usage from main.py:

        from .logging_loki import loki

        loki.log("info", {"event_type": "health"}, service_type="orchestrator")

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
        )
    """

    def __init__(self) -> None:
        self.url = os.getenv("GRAFANA_LOKI_URL")
        self.username = os.getenv("GRAFANA_LOKI_USERNAME")
        self.token = os.getenv("GRAFANA_LOKI_API_TOKEN")
        self.app_label = os.getenv("MCP_APP_LABEL", "mcp_orchestrator_sync")

        self.enabled = all([self.url, self.username, self.token])
        if not self.enabled:
            print("[LokiLogger] Disabled – missing GRAFANA_LOKI_* env vars")
        else:
            print("[LokiLogger] Enabled, pushing to", self.url)

    # ----------------- internal helpers -----------------

    def _build_stream_labels(self, level: str, fields: dict) -> dict:
        """
        Build Loki 'stream' labels.

        LOW-cardinality labels only:
          - app
          - level
          - event
          - service
          - flow
          - step
          - intent
          - outcome

        High-card items like session_id remain in JSON body.
        """
        labels = {
            "app": self.app_label,
            "level": level,
        }

        # Event name (e.g. input/output/error/health/request_start/request_end)
        event = fields.get("event") or fields.get("event_type")
        if event:
            labels["event"] = str(event)

        # Promote a few keys as Loki labels if present
        mapping = {
            "service_type": "service",
            "service": "service",
            "flow": "flow",
            "step": "step",
            "intent": "intent",
            "outcome": "outcome",
        }
        for src, dst in mapping.items():
            val = fields.get(src)
            if val not in (None, "", []):
                labels[dst] = str(val)

        return labels

    # ----------------- public API -----------------

    def log(self, level: str, message, **fields) -> None:
        """
        Main logging function used by the rest of the app.

        level   : "info", "warning", "error", etc.
        message : str OR dict
        fields  : extra context: event, flow, step, service_type, session_id, etc.
        """
        if not self.enabled:
            return

        # Build structured JSON body
        if isinstance(message, dict):
            payload_fields = {**fields, **message}
        else:
            payload_fields = {**fields, "message": str(message)}

        ts_ns = int(time.time() * 1_000_000_000)  # nanoseconds

        stream_labels = self._build_stream_labels(level, payload_fields)

        body = {
            "streams": [
                {
                    "stream": stream_labels,
                    "values": [
                        [str(ts_ns), json.dumps(payload_fields, ensure_ascii=False)]
                    ],
                }
            ]
        }

        try:
            resp = requests.post(
                self.url,
                auth=(self.username, self.token),
                json=body,
                timeout=4,
            )
            if resp.status_code not in (200, 204):
                print(
                    "[LokiLogger] Push failed:",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as e:
            print("[LokiLogger] Exception while pushing to Loki:", e)


# Global logger used by main.py
loki = LokiLogger()
