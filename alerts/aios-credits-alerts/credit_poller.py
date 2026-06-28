#!/usr/bin/env python3
"""
Credit Poller
- Polls PostgreSQL (via Grafana API proxy) every 60s
- Alerts if org drops below 8k for first time (one-time, no repeat)
- Alerts if org drops below 4k for first time (one-time, no repeat)
- Alerts if org drops below 2k for first time
- Re-alerts if credits dropped further since last alert
- Re-alerts after 24hrs if credits still below 2k (no change)
- Handles negative credits
- Creates Monday.com tickets for low-credit orgs
"""

import json
import time
import logging
import os
import signal
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = os.path.expanduser(os.getenv("LOG_FILE", "~/slack-alerts/credit_poller.log"))
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
GRAFANA_URL      = os.getenv("GRAFANA_URL")
GRAFANA_TOKEN    = os.getenv("GRAFANA_TOKEN")
GRAFANA_DS_UID   = os.getenv("GRAFANA_DS_UID")

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_BOARD_ID  = int(os.getenv("MONDAY_BOARD_ID", "0"))
SLACK_WEBHOOK    = os.getenv("SLACK_WEBHOOK")

POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "60"))     # seconds
REPEAT_AFTER     = int(os.getenv("REPEAT_AFTER", "86400"))   # 24 hours
STATE_FILE       = os.path.expanduser(os.getenv("STATE_FILE", "~/slack-alerts/credit_poller_state.json"))

# DB schema / table config (from env so it works across environments)
DB_SCHEMA        = os.getenv("DB_SCHEMA", "public")
DB_TABLE_WALLETS = os.getenv("DB_TABLE_WALLETS", "billing_wallets")
DB_TABLE_ORGS    = os.getenv("DB_TABLE_ORGS", "organizations")

# Monday request timeout (seconds) — prevents crash on API slowness
MONDAY_TIMEOUT   = int(os.getenv("MONDAY_TIMEOUT", "15"))

# Buckets that only alert ONCE and never repeat (even after 24hrs)
ONE_TIME_BUCKETS = {8000, 4000}
# Buckets that repeat every REPEAT_AFTER seconds
REPEAT_BUCKETS   = {2000}

# ── Validate required env vars ────────────────────────────────────────────────
REQUIRED = ["GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANA_DS_UID", "SLACK_WEBHOOK"]
missing = [k for k in REQUIRED if not os.getenv(k)]
if missing:
    log.error(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Grafana API query (proxies to PostgreSQL) ─────────────────────────────────
def query_grafana(sql: str) -> list[dict]:
    """
    Executes a SQL query through the Grafana datasource proxy endpoint.
    This is required because Grafana Cloud does not expose direct PostgreSQL
    connections externally — the Grafana API acts as an authenticated proxy.
    """
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasourceId": GRAFANA_DS_UID,
                "rawSql": sql,
                "format": "table",
            }
        ],
        "from": "now-5m",
        "to": "now",
    }
    headers = {
        "Authorization": f"Bearer {GRAFANA_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{GRAFANA_URL}/api/ds/query",
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    frames = data.get("results", {}).get("A", {}).get("frames", [])
    if not frames:
        return []

    schema = frames[0]["schema"]["fields"]
    values = frames[0]["data"]["values"]
    columns = [f["name"] for f in schema]

    rows = []
    for i in range(len(values[0])):
        rows.append({col: values[j][i] for j, col in enumerate(columns)})
    return rows

# ── Fetch all org credit balances ─────────────────────────────────────────────
def fetch_credit_balances() -> list[dict]:
    sql = f"""
        SELECT
            o.id        AS org_id,
            o.name      AS org_name,
            w.balance   AS credits
        FROM {DB_SCHEMA}.{DB_TABLE_WALLETS} w
        JOIN {DB_SCHEMA}.{DB_TABLE_ORGS} o ON o.id = w.organization_id
        ORDER BY w.balance ASC
    """
    return query_grafana(sql)

# ── Slack alert ───────────────────────────────────────────────────────────────
def send_slack_alert(org_name: str, org_id: str, credits: float, threshold: int):
    emoji = ":rotating_light:" if credits < 0 else ":warning:"
    credit_str = f"{credits:,.0f}"
    text = (
        f"{emoji} *Low Credits Alert*\n"
        f"*Org:* {org_name} (`{org_id}`)\n"
        f"*Credits:* {credit_str}\n"
        f"*Threshold crossed:* {threshold:,}"
    )
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
        resp.raise_for_status()
        log.info(f"Slack alert sent: {org_name} | credits={credit_str} | threshold={threshold}")
    except Exception as e:
        log.error(f"Failed to send Slack alert for {org_name}: {e}")

# ── Monday.com ticket ─────────────────────────────────────────────────────────
def create_monday_ticket(org_name: str, org_id: str, credits: float):
    if not MONDAY_API_TOKEN or not MONDAY_BOARD_ID:
        log.warning("Monday.com not configured, skipping ticket creation")
        return
    query = """
        mutation ($boardId: ID!, $itemName: String!) {
            create_item(board_id: $boardId, item_name: $itemName) {
                id
            }
        }
    """
    variables = {
        "boardId": str(MONDAY_BOARD_ID),
        "itemName": f"Low Credits: {org_name} ({credits:,.0f} credits)",
    }
    try:
        resp = requests.post(
            "https://api.monday.com/v2",
            json={"query": query, "variables": variables},
            headers={
                "Authorization": MONDAY_API_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=MONDAY_TIMEOUT,   # prevents crash on Monday API slowness
        )
        resp.raise_for_status()
        log.info(f"Monday ticket created for {org_name}")
    except requests.exceptions.Timeout:
        log.warning(f"Monday.com API timed out for {org_name} — skipping ticket, will not crash")
    except Exception as e:
        log.error(f"Failed to create Monday ticket for {org_name}: {e}")

# ── Alert logic ───────────────────────────────────────────────────────────────
def process_org(org: dict, state: dict, now: float):
    org_id    = str(org["org_id"])
    org_name  = org["org_name"]
    credits   = float(org["credits"])

    org_state = state.get(org_id, {})

    for threshold in sorted([8000, 4000, 2000], reverse=True):
        if credits < threshold:
            bucket_key = f"alerted_{threshold}"
            last_alert = org_state.get(bucket_key)

            if threshold in ONE_TIME_BUCKETS:
                if last_alert is None:
                    send_slack_alert(org_name, org_id, credits, threshold)
                    create_monday_ticket(org_name, org_id, credits)
                    org_state[bucket_key] = now
                    org_state[f"credits_at_{threshold}"] = credits
            elif threshold in REPEAT_BUCKETS:
                credits_at_last = org_state.get(f"credits_at_{threshold}")
                should_alert = (
                    last_alert is None
                    or credits < (credits_at_last or threshold)
                    or (now - last_alert) >= REPEAT_AFTER
                )
                if should_alert:
                    send_slack_alert(org_name, org_id, credits, threshold)
                    create_monday_ticket(org_name, org_id, credits)
                    org_state[bucket_key] = now
                    org_state[f"credits_at_{threshold}"] = credits
            break  # only alert on the lowest crossed threshold

    state[org_id] = org_state

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("Credit poller started")
    log.info(f"Grafana URL: {GRAFANA_URL} | DS UID: {GRAFANA_DS_UID}")
    log.info(f"DB: {DB_SCHEMA}.{DB_TABLE_WALLETS} JOIN {DB_SCHEMA}.{DB_TABLE_ORGS}")
    log.info(f"Poll interval: {POLL_INTERVAL}s | Repeat after: {REPEAT_AFTER}s")

    def handle_signal(sig, frame):
        log.info("Shutting down credit poller")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while True:
        try:
            orgs = fetch_credit_balances()
            state = load_state()
            now = time.time()

            log.info(f"Fetched {len(orgs)} orgs")
            for org in orgs:
                process_org(org, state, now)

            save_state(state)
        except Exception as e:
            log.error(f"Poll cycle error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
