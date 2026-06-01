"""
core — Zentrale Infrastruktur (DB, Logging, Dashboard).

Vorgesehen für spätere Container-Trennung:
  - core.db        → DB-Container (SQLite jetzt, Postgres später)
  - core.logging   → Logger-Service-Container (TCP)
  - core.dashboard → Web-Dashboard-Container (FastAPI)

Worker importieren nur die Client-APIs:
  from core.logging import get_logger
  from core.db import deals_repo, state_repo, workers_repo
"""
