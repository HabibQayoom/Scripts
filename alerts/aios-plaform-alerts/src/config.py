#!/usr/bin/env python3
"""
Shared configuration loader — reads all settings from environment / .env
Used by both the Loki and Azure Monitor editions.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Load .env from the repo root (parent of src/)
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)
except ImportError:
    # python-dotenv not installed — rely on real environment variables
    pass


def _req(name: str) -> str:
    """Required env var — raise a clear error if missing."""
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(
            f"Missing required environment variable: {name}\n"
            f"Copy .env.example to .env and fill in the values."
        )
    return val


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ── Grafana ──────────────────────────────────────────────────────────────────
GRAFANA_URL   = _req("GRAFANA_URL").rstrip("/")
GRAFANA_TOKEN = _req("GRAFANA_TOKEN")

# ── Loki ─────────────────────────────────────────────────────────────────────
LOKI_DS_ID = _opt("LOKI_DS_ID", "12")

# ── Azure Monitor ────────────────────────────────────────────────────────────
AZURE_DS_UID   = _opt("AZURE_DS_UID")
AZURE_RESOURCE = _opt("AZURE_RESOURCE")

# ── K8s scope ────────────────────────────────────────────────────────────────
NAMESPACE = _opt("NAMESPACE", "ai-os-production")
CLUSTER   = _opt("CLUSTER", "")  # may be blank if label removed

# ── Slack ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = _req("SLACK_BOT_TOKEN")
ALERT_CHANNEL   = _req("ALERT_CHANNEL")
REPORT_CHANNEL  = _req("REPORT_CHANNEL")

# ── Behaviour ────────────────────────────────────────────────────────────────
POLL_SECONDS    = int(_opt("POLL_SECONDS", "30"))
INGEST_LAG_SEC  = int(_opt("INGEST_LAG_SEC", "180"))
UI_PORT         = int(_opt("UI_PORT", "9002"))
COUNTS_MAX_DAYS = int(_opt("COUNTS_MAX_DAYS", "15"))
MAX_LOG_BYTES   = int(_opt("MAX_LOG_MB", "300")) * 1024 * 1024
MAX_LOG_DAYS    = int(_opt("MAX_LOG_DAYS", "20"))

# ── Storage ──────────────────────────────────────────────────────────────────
DATA_DIR = Path(_opt("DATA_DIR", "./data")).expanduser().resolve()

# Application containers to monitor (Azure edition filters by these)
APP_CONTAINERS = [
    "fastapi", "rabbitmq", "redis-app", "redis-redbeat",
    "celery-analyst-team-worker", "celery-command-team-worker",
    "celery-computer-use-team-worker", "celery-flower",
    "celery-integrations-worker", "celery-maintenance-worker",
    "celery-notifications-worker", "celery-post-interaction-worker",
    "celery-redbeat", "celery-task-team-worker",
]
