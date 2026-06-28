# AIOS Alert Manager

Real-time error alerting for the AIOS platform. Polls logs from **Grafana Loki** or **Azure Monitor**, posts each unique error to Slack, and provides a web UI to silence noisy errors and send on-demand error reports.

Two editions are included because AIOS production log shipping migrated from Loki to Azure Monitor:

| Edition | Data source | File | When to use |
|---|---|---|---|
| **Azure Monitor** | `ContainerLogV2` via Grafana Azure Monitor datasource | `src/aios-application-alerts-azure.py` | Current production — logs ship to Azure Monitor |
| **Loki** | Grafana Cloud Loki via datasource proxy | `src/aios-application-alerts-loki.py` | Legacy / clusters still shipping to Loki |

Both editions share identical Slack, UI, silencing, grouping, and reporting logic — only the data layer differs.

---

## Features

- **Real-time error alerts** — each unique error is posted to a Slack channel as it appears.
- **No spam** — an error is alerted **once per run**. Repeats are silently counted, never re-posted.
- **Smart grouping** — errors are normalized (Task IDs, UUIDs, timestamps, PIDs, IPs, hex addresses, ANSI codes stripped) so the same error with different identifiers groups into one entry.
- **Web UI** (default port `9002`):
  - **Last 24 Hours / Since Restart / Last 3 Days** views
  - **Silence** any error group for a custom duration (hours / days / forever)
  - **Expandable occurrences** — click a group to see individual log lines with pod names and timestamps
  - **Review & send report** — pick which errors to include, add comments, send to a Slack channel on demand
- **Auto cleanup** — error counts older than `COUNTS_MAX_DAYS` are purged; the log file rotates at `MAX_LOG_MB` / `MAX_LOG_DAYS`.
- **Crash-safe** — cursor state is persisted so restarts don't miss or duplicate logs.

---

## Quick start

```bash
# 1. Clone and enter
git clone <your-repo-url> aios-alerts
cd aios-alerts

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env          # fill in tokens, channel IDs, datasource IDs

# 4. Run (pick one edition)
python3 src/aios-application-alerts-azure.py     # Azure Monitor
# or
python3 src/aios-application-alerts-loki.py      # Loki
```

The web UI is then available at `http://<host>:9002`.

---

## Configuration

All settings live in `.env` (copied from `.env.example`). Secrets are **never** committed — `.env` is in `.gitignore`.

### Required

| Variable | Description |
|---|---|
| `GRAFANA_URL` | Your Grafana stack URL, e.g. `https://your-stack.grafana.net` |
| `GRAFANA_TOKEN` | Grafana service account token (`glsa_…`) |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-…`), scopes: `chat:write`, `channels:read`, `groups:read` |
| `ALERT_CHANNEL` | Slack channel **ID** for live alerts |
| `REPORT_CHANNEL` | Slack channel **ID** for the manual report |

### Azure Monitor edition

| Variable | Description |
|---|---|
| `AZURE_DS_UID` | UID of the Azure Monitor datasource in Grafana |
| `AZURE_RESOURCE` | Full Log Analytics workspace resource ID |
| `INGEST_LAG_SEC` | Ingestion delay buffer (default `180`). Azure logs take ~60–120 s to appear, so the script queries up to this many seconds in the past to avoid skipping un-ingested logs. |

### Loki edition

| Variable | Description |
|---|---|
| `LOKI_DS_ID` | Numeric Loki datasource ID |
| `CLUSTER` | Loki `cluster` label value. **Leave blank** if the label has been removed from your logs. |

### Common / optional

| Variable | Default | Description |
|---|---|---|
| `NAMESPACE` | `ai-os-production` | Kubernetes namespace to monitor |
| `POLL_SECONDS` | `30` | Seconds between polls |
| `UI_PORT` | `9002` | Web UI port |
| `COUNTS_MAX_DAYS` | `15` | Auto-delete error entries older than this |
| `MAX_LOG_MB` | `300` | Rotate log file above this size |
| `MAX_LOG_DAYS` | `20` | Rotate log file older than this |
| `DATA_DIR` | `./data` | Where state, counts, silence, and log files are stored |

---

## How it works

```
┌─────────────┐   poll every    ┌──────────────────┐   each unique    ┌─────────┐
│ Loki /      │ ──────────────► │  Alert Manager   │ ───── error ───► │  Slack  │
│ Azure Mon.  │   30s window    │  (this script)   │   posted once    │ channel │
└─────────────┘                 └──────────────────┘                  └─────────┘
                                     │        ▲
                            counts   │        │  silence / report
                            +samples ▼        │  actions
                                ┌──────────────────┐
                                │   Web UI :9002   │
                                └──────────────────┘
```

1. Every `POLL_SECONDS`, the script queries the data source for new error/critical/fatal logs.
2. Each log is normalized into a stable **error key**. The first time a key is seen this run, it's posted to Slack; afterwards it's only counted.
3. All occurrences are stored in `daily-error-counts.json` with up to 20 sample lines per group, powering the UI and report.
4. Silenced keys (set via the UI) are counted but never alerted until the silence expires.

### Azure Monitor ingestion lag

Azure Monitor's `ContainerLogV2` table has a real ingestion delay (typically 60–120 s). If the script queried right up to "now", those logs wouldn't be in the table yet and the cursor would move past them, skipping them forever. To prevent this, the Azure edition queries up to `INGEST_LAG_SEC` seconds in the past. The trade-off is that alerts arrive a few minutes after the error occurs.

The Loki edition has no such delay and alerts in near real-time.

---

## Running as a background service

### nohup (simple)

```bash
nohup python3 src/aios-application-alerts-azure.py >> data/run.out 2>&1 &
echo $! > data/run.pid
```

### systemd (recommended for production)

Create `/etc/systemd/system/aios-alerts.service`:

```ini
[Unit]
Description=AIOS Alert Manager
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/aios-alerts
ExecStart=/usr/bin/python3 /home/youruser/aios-alerts/src/aios-application-alerts-azure.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aios-alerts
sudo journalctl -u aios-alerts -f
```

---

## Project layout

```
aios-alerts/
├── .env.example          # template — copy to .env
├── .gitignore            # keeps .env and runtime data out of git
├── requirements.txt
├── README.md
└── src/
    ├── config.py                          # loads settings from .env
    ├── aios-application-alerts-azure.py    # Azure Monitor edition
    └── aios-application-alerts-loki.py     # Loki edition
```

Runtime files (created on first run, all ignored by git):

```
data/
├── aios-application-alerts.log     # the script's own log
├── aios-application-alerts.state   # cursor (last processed timestamp)
├── silenced-groups.json            # active silences
├── daily-error-counts.json         # error counts + samples
└── view-restart.txt                # "Since Restart" view marker
```

---

## Security

- All secrets live in `.env`, which is git-ignored. Never commit real tokens.
- If a token is ever exposed, rotate it immediately:
  - **Grafana:** Administration → Service accounts → revoke & recreate the token
  - **Slack:** api.slack.com/apps → your app → OAuth & Permissions → regenerate

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `POLL … 0 entries` but the dashboard shows errors (Azure) | Ingestion lag — querying logs that haven't landed yet | Ensure `INGEST_LAG_SEC` is ≥ your measured lag (check with `datetime_diff("second", ingestion_time(), TimeGenerated)`) |
| `0 entries` on Loki edition | `cluster` label removed from logs, or logs moved to Azure | Blank out `CLUSTER` in `.env`; if logs no longer ship to Loki, switch to the Azure edition |
| `Address already in use` on start | Previous instance still bound to `UI_PORT` | `fuser -k <port>/tcp` or set a different `UI_PORT` |
| Missing env var error on start | `.env` not filled in | `cp .env.example .env` and complete it |
| Alerts duplicated after restart | Expected — the "alert once" guard resets each run | Use silences for permanently-known errors |
