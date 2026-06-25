import json
import os
import time
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Configuration (override via .env or env vars) ──
COMPOSIO_API_URL = os.getenv(
    "COMPOSIO_API_URL",
    "https://backend.composio.dev/api/v3/internal/action_execution/logs"
)
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "100"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))

if not COMPOSIO_API_KEY:
    raise EnvironmentError("COMPOSIO_API_KEY is not set. Check your .env file.")

# File paths
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/composio"))
LOG_FILE = LOG_DIR / "executions.log"
SEEN_FILE = LOG_DIR / "seen_ids.json"

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("composio-collector")


def ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_seen_ids():
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return set(data.get("ids", []))
    return set()


def save_seen_ids(ids):
    # Keep last 10000 IDs to prevent file growing forever
    recent = list(ids)[-10000:]
    SEEN_FILE.write_text(json.dumps({"ids": recent}))


def fetch_logs(cursor=None):
    headers = {
        "x-api-key": COMPOSIO_API_KEY,
        "Content-Type": "application/json"
    }
    # cursor=0 gets latest logs, nextCursor goes backward (older)
    body = {"cursor": cursor if cursor else 0, "limit": FETCH_LIMIT}

    resp = requests.post(COMPOSIO_API_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    entries = data.get("data", [])
    next_cursor = data.get("nextCursor")
    return entries, next_cursor


def format_log_entry(entry):
    """Format a Composio log entry as a structured JSON line."""
    return json.dumps({
        "timestamp": datetime.fromtimestamp(
            entry["createdAt"] / 1000, tz=timezone.utc
        ).isoformat(),
        "id": entry.get("id"),
        "action": entry.get("actionKey"),
        "app": entry.get("appKey"),
        "status": entry.get("status"),
        "executionTime": entry.get("executionTime"),
        "entityId": entry.get("entityId"),
        "connectedAccountId": entry.get("connectedAccountId"),
        "metadata": entry.get("metadata", {}),
    })


def deduplicate_log_file():
    """Remove duplicate entries from the log file based on log ID."""
    if not LOG_FILE.exists():
        return
    seen = set()
    unique_lines = []
    with open(LOG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                eid = entry.get("id")
                if eid and eid not in seen:
                    seen.add(eid)
                    unique_lines.append(line)
            except json.JSONDecodeError:
                continue
    with open(LOG_FILE, "w") as f:
        for line in unique_lines:
            f.write(line + "\n")
    return len(unique_lines)


def append_to_log(entries, seen_ids):
    """Append new entries to log file. Returns (new_count, hit_duplicate)."""
    new_count = 0
    hit_dup = False
    with open(LOG_FILE, "a") as f:
        for entry in entries:
            eid = entry.get("id")
            if eid in seen_ids:
                hit_dup = True
                continue
            seen_ids.add(eid)
            f.write(format_log_entry(entry) + "\n")
            new_count += 1
    return new_count, hit_dup


def run():
    ensure_dirs()
    seen_ids = load_seen_ids()
    log.info("Starting collection. seen_ids=%d", len(seen_ids))

    total_new = 0
    cursor = None  # Start from latest (cursor=0)

    for page in range(1, MAX_PAGES + 1):
        try:
            entries, next_cursor = fetch_logs(cursor)
        except Exception as e:
            log.error("Failed to fetch page %d: %s", page, e)
            break

        if not entries:
            log.info("No entries on page %d. Done.", page)
            break

        new, hit_dup = append_to_log(entries, seen_ids)
        total_new += new
        log.info("Page %d: fetched %d, new %d (total new: %d)", page, len(entries), new, total_new)

        # If we hit duplicates, we've caught up — stop
        if hit_dup:
            log.info("Hit already-seen logs. Caught up.")
            break

        # Move to next (older) page
        cursor = next_cursor

        if not next_cursor or len(entries) < FETCH_LIMIT:
            break

        time.sleep(0.3)

    save_seen_ids(seen_ids)
    log.info("Done. %d new entries written to %s", total_new, LOG_FILE)


if __name__ == "__main__":
    run()
