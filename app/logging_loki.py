
import os
import time
import requests

class LokiLogger:
    """
    Small helper to push logs to Grafana Cloud Loki.

    Uses env vars:
      - GRAFANA_LOKI_URL      (e.g. https://logs-prod-025.grafana.net/loki/api/v1/push)
      - GRAFANA_LOKI_USERNAME (tenant / user ID)
      - GRAFANA_LOKI_API_TOKEN (API token with logs:write)
    """
    def __init__(self):
        self.url = os.getenv("GRAFANA_LOKI_URL")
        self.username = os.getenv("GRAFANA_LOKI_USERNAME")
        self.token = os.getenv("GRAFANA_LOKI_API_TOKEN")
        self.enabled = all([self.url, self.username, self.token])
        if not self.enabled:
            print("[LokiLogger] Disabled (missing env vars).")
        else:
            print("[LokiLogger] Enabled, pushing to", self.url)

    def log(self, level, message, **labels):
        if not self.enabled:
            return
        stream_labels = {"app": "mcp_orchestrator_sync", "level": level}
        stream_labels.update({k: str(v) for k, v in labels.items() if v is not None})
        ts_ns = int(time.time() * 1e9)
        payload = {
            "streams": [
                {
                    "stream": stream_labels,
                    "values": [[str(ts_ns), message]]
                }
            ]
        }
        try:
            resp = requests.post(self.url, auth=(self.username, self.token), json=payload, timeout=4)
            if resp.status_code not in (200, 204):
                print("[LokiLogger] Push failed:", resp.status_code, resp.text[:200])
        except Exception as e:
            print("[LokiLogger] Exception:", e)

loki = LokiLogger()
