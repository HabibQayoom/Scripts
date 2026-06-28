# Credit Poller

A Python daemon that monitors organisation credit balances on the AIOS platform and sends Slack alerts when balances fall below defined thresholds. It also creates Monday.com tickets for low-credit organisations for the customer success team to action.

---

## Why This Exists — Grafana Limitations

Grafana Cloud's alerting engine evaluates rules on a **scheduled interval** (minimum 1 minute) and is primarily designed for time-series metric data. This script exists because of several constraints that make native Grafana alerting insufficient for this use case:

| Limitation | Impact |
|---|---|
| Grafana alerts fire every evaluation cycle if the condition is true | No way to enforce "alert only once per threshold crossed" natively |
| No built-in state tracking between alert firings | Cannot detect "credits dropped further since last alert" |
| Grafana cannot conditionally suppress repeat alerts (e.g. one-time vs 24h repeat) | 8k and 4k thresholds must only alert once, ever — Grafana cannot do this |
| No native Monday.com integration in Grafana alerting | Ticket creation requires custom code |
| Grafana alert routing does not support per-org logic | Each org needs independent state to avoid alert storms |

This script fills that gap by maintaining its own state file and implementing precise per-threshold, per-organisation alert logic.

---

## How It Works

1. Every `POLL_INTERVAL` seconds, the script queries the PostgreSQL database **via the Grafana API proxy** (not a direct DB connection — see below).
2. It fetches the current credit balance for every organisation by joining `credits-table` and `org-table` tables.
3. For each org, it checks which thresholds have been crossed and whether an alert should fire based on the rules below.
4. Alert state is persisted to a local JSON file (`STATE_FILE`) so restarts do not cause duplicate alerts.

### Alert Rules

| Threshold | Behaviour |
|---|---|
| **8,000 credits** | Alert once only. Never repeats, even after 24 hours. |
| **4,000 credits** | Alert once only. Never repeats, even after 24 hours. |
| **2,000 credits** | Alert on first crossing. Re-alerts if credits drop further. Re-alerts every 24 hours if balance remains below 2,000 with no change. |

Only the **lowest crossed threshold** triggers an alert per poll cycle. If an org is at 1,500 credits, only the 2,000 threshold fires (not 8,000 and 4,000 again).

### Why Grafana API and Not a Direct DB Connection

Grafana Cloud does not expose the managed PostgreSQL instance with a public connection string. The database is only accessible through the Grafana datasource proxy at `/api/ds/query`. This endpoint accepts raw SQL, authenticates via a Grafana service account token, and returns results in a structured frame format. The script parses this response and works with it like a normal query result.

This also means **no DB credentials are needed** — only a Grafana service account token with datasource query permissions.

---

## Project Structure

```
credit-poller/
├── credit_poller.py     # Main daemon script
├── .env                 # Real credentials 
├── .env.example         # Template for new environments
├── requirements.txt     # Python dependencies
└── README.md
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd credit-poller
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env   # fill in your values
```

Required variables:

| Variable | Description |
|---|---|
| `GRAFANA_URL` | Your Grafana Cloud instance URL (e.g. `https://iai.grafana.net`) |
| `GRAFANA_TOKEN` | Grafana service account token with datasource query permission |
| `GRAFANA_DS_UID` | UID of the PostgreSQL datasource in Grafana |
| `SLACK_WEBHOOK` | Incoming webhook URL for the target Slack channel |

Optional variables:

| Variable | Default | Description |
|---|---|---|
| `MONDAY_API_TOKEN` | *(empty)* | Monday.com API token — leave blank to disable ticket creation |
| `MONDAY_BOARD_ID` | *(empty)* | Monday.com board ID where tickets are created |
| `MONDAY_TIMEOUT` | `15` | Request timeout in seconds for Monday.com API calls |
| `DB_SCHEMA` | `public` | PostgreSQL schema name |
| `DB_TABLE_WALLETS` | `credits-table` | Wallet balances table name |
| `DB_TABLE_ORGS` | `org-table` | Organisations table name |
| `POLL_INTERVAL` | `60` | How often to poll the database, in seconds |
| `REPEAT_AFTER` | `86400` | Seconds before re-alerting for persistent low credits (default: 24h) |
| `LOG_FILE` | `~/slack-alerts/credit_poller.log` | Log file path |
| `STATE_FILE` | `~/slack-alerts/credit_poller_state.json` | Alert state persistence file |

### 3. Run

**Foreground (testing):**
```bash
python3 credit_poller.py
```

**Background with nohup:**
```bash
nohup python3 credit_poller.py >> ~/slack-alerts/credit_poller.log 2>&1 &
echo $! > ~/slack-alerts/credit_poller.pid
```

**Start on reboot via crontab:**
```bash
crontab -e
```
Add:
```
@reboot cd /path/to/credit-poller && nohup python3 credit_poller.py >> ~/slack-alerts/credit_poller.log 2>&1 &
```

---

## Checking Status

```bash
# Is it running?
ps aux | grep credit_poller

# View logs
tail -f ~/slack-alerts/credit_poller.log

# View current alert state
cat ~/slack-alerts/credit_poller_state.json | python3 -m json.tool
```

## Stopping

```bash
kill $(cat ~/slack-alerts/credit_poller.pid)
```

---

## Database Tables Used

The script queries two tables via the Grafana datasource proxy:

**`credits-table`** — stores the credit balance per organisation
```sql
credits-table (
    id,
    org_id,   -- FK to org.id
    balance            -- current credit balance (float)
)
```

**`org-table`** — stores org metadata
```sql
org-table (
    id,
    name               -- display name used in Slack alerts
)
```

The query joins these two tables and orders results by balance ascending so the lowest-credit orgs are always evaluated first.

---

## Known Issues / Notes

- **Monday.com timeouts:** The Monday.com API occasionally returns `ReadTimeoutError`. This is handled gracefully — the script logs a warning and continues. It does **not** crash. The `MONDAY_TIMEOUT` env var controls how long to wait.
- **State file:** If you delete the state file, all one-time alerts (8k, 4k) will fire again for all orgs currently below those thresholds on the next poll cycle.
- **Negative credits:** Handled correctly — orgs with negative balances are treated as below all thresholds.
