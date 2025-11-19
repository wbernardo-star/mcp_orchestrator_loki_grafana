# MCP Orchestrator – Sync + Full Food Flow + Loki + Grafana

This repo contains:
- Synchronous MCP Orchestrator with FULL food-order state machine
- Loki logging integration (Grafana Cloud)
- Prometheus metrics (`/metrics`)
- Grafana dashboard JSON for Loki logs

Endpoints:
- `GET /health`
- `GET /services`
- `GET /metrics`
- `POST /orchestrate`

Deployable on Railway using Procfile.
