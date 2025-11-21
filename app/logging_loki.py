# app/logging_loki.py
import os
import time
import json
from typing import Any, Dict, Optional

import requests


class LokiLogger:
    def __init__(
        self,
        endpoint: Optional[str] = None,
        app_name: str = "mcp_orchestrator_sync",
    ) -> None:
        self.endpoint = endpoint or os.getenv("LOKI_ENDPOINT")
        self.basic_auth = os.getenv("LOKI_BASIC_AUTH")  # e.g. "user:token"
        self.app_name = app_name

        if self.endpoint:
            print(f"[LokiLogger] Enabled, pushing to {self.endpoint}")
        else:
            print("[LokiLogger] Disabled (no LOKI_ENDPOINT set)")

    def log(self, level: str, payload: Dict[str, Any], **labels: Any) -> None:
        """
        level   : 'info' | 'error' | ...
        payload : JSON dict that will be stored as the log line
        labels  : extra Loki labels (service_type, sync_mode, io, event, etc.)
        """
        # If Loki not configured, just print to stdout and bail.
        if not self.endpoint:
            print(f"[{level.upper()}]", payload, labels)
            return

        # Base labels
        event_type = payload.get("event_type", "unknown")
        base_labels: Dict[str, str] = {
            "app": self.app_name,
            "level": level,
            "event": event_type,
        }

        # Merge extra labels (coerce to str, skip None)
        for k, v in labels.items():
            if v is not None:
                base_labels[k] = str(v)

        # Loki expects nanoseconds since epoch
        ts_ns = int(time.time() * 1_000_000_000)

        line = json.dumps(payload, separators=(",", ":"))
        body = {
            "streams": [
                {
                    "stream": base_labels,
                    "values": [[str(ts_ns), line]],
                }
            ]
        }

        headers = {"Content-Type": "application/json"}
        if self.basic_auth:
            # basic_auth is "user:password" → Base64 via requests.auth?
            # Loki/Grafana Cloud also accepts it via HTTP header:
            headers["Authorization"] = f"Basic {self.basic_auth}"

        try:
            requests.post(
                self.endpoint,
                data=json.dumps(body),
                headers=headers,
                timeout=1.0,
            )
        except Exception as e:
            # Don't crash the app if logging fails
            print("[LokiLogger] Push failed:", e)


# Singleton instance used everywhere
loki = LokiLogger()
