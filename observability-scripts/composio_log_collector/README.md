# composio-loki-bridge

A lightweight polling collector that pulls action execution logs from the [Composio](https://composio.dev) API and writes structured JSON logs to disk, where [Grafana Alloy](https://grafana.com/docs/alloy/) picks them up and ships them to **Grafana Loki** for visualization in Grafana dashboards.

---

## What It Does

- Polls the Composio API for the latest action execution logs every 5 minutes (via cron)
- Writes one JSON log line per execution to `/var/log/composio/executions.log`
- Tracks already-seen log IDs in `seen_ids.json` to avoid duplicates across runs
- Automatically stops paginating once it hits previously seen logs (catch-up mechanism)
- Keeps the seen IDs file capped at 10,000 entries to prevent unbounded growth

---

## Folder Structure

```
composio-loki-bridge/
├── composio_log_collector.py   # Main collector script
├── .env.example                # Environment variable template (copy to .env)
├── .gitignore                  # Excludes .env and seen_ids.json from version control
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

---

## Requirements

- Python 3.8+
- `pip install -r requirements.txt`

---

## Setup

### 1. Clone the repo and install dependencies

```bash
git clone <your-repo-url>
cd composio-loki-bridge
pip3 install -r requirements.txt --break-system-packages
```

### 2. Configure environment variables

```bash
cp .env.example .env
nano .env
```

Fill in your actual credentials in `.env`:

```env
COMPOSIO_API_URL=https://backend.composio.dev/api/v3/internal/action_execution/logs
COMPOSIO_API_KEY=ak_...
LOG_DIR=/var/log/composio
```

> **Never commit `.env` to git.** It is listed in `.gitignore`.

### 3. Create log directory

```bash
sudo mkdir -p /var/log/composio
sudo chown $USER:$USER /var/log/composio
```

### 4. Run manually to test

```bash
python3 composio_log_collector.py
```

Expected output:

```
2026-06-23 13:00:00 [INFO] Starting collection. seen_ids=0
2026-06-23 13:00:01 [INFO] Page 1: fetched 100, new 100 (total new: 100)
2026-06-23 13:00:01 [INFO] Page 2: fetched 100, new 100 (total new: 200)
2026-06-23 13:00:02 [INFO] Hit already-seen logs. Caught up.
2026-06-23 13:00:02 [INFO] Done. 200 new entries written to /var/log/composio/executions.log
```

### 5. Set up cron (every 5 minutes)

```bash
crontab -e
```

Add this line:

```
*/5 * * * * flock -n /tmp/composio.lock /usr/bin/python3 /home/<your-user>/composio-loki-bridge/composio_log_collector.py >> /var/log/composio/collector.log 2>&1
```

> `flock` ensures only one instance runs at a time.

---

## Log Output Format

### executions.log (one line per action execution)

```json
{
  "timestamp": "2026-06-23T13:00:01.000000+00:00",
  "id": "exec-uuid-here",
  "action": "GITHUB_CREATE_ISSUE",
  "app": "github",
  "status": "success",
  "executionTime": 342,
  "entityId": "user-uuid",
  "connectedAccountId": "account-uuid",
  "metadata": {}
}
```

---

## Deduplication

The script tracks seen log IDs in:

```
/var/log/composio/seen_ids.json
```

This file is auto-managed — no manual intervention needed. It is listed in `.gitignore` and should never be committed to git.

---

## Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `COMPOSIO_API_URL` | Composio backend URL | API endpoint to poll |
| `COMPOSIO_API_KEY` | *(required)* | Your Composio API key |
| `LOG_DIR` | `/var/log/composio` | Directory for log output |
| `FETCH_LIMIT` | `100` | Entries per page |
| `MAX_PAGES` | `10` | Max pages per run |

---

## Grafana Alloy Configuration

Alloy must be configured to tail the log file and ship it to Loki:

```hcl
local.file_match "composio_logs" {
  path_targets = [{
    __path__ = "/var/log/composio/executions.log"
  }]
}

loki.source.file "composio" {
  targets    = local.file_match.composio_logs.targets
  forward_to = [loki.write.default.receiver]
}
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `EnvironmentError: COMPOSIO_API_KEY is not set` | `.env` file missing or key not set — check against `.env.example` |
| Duplicate entries in log | Run `deduplicate_log_file()` manually or delete `seen_ids.json` and re-run |
| No new entries being written | `seen_ids.json` may be stale — delete it to force a fresh collection |
| API returning 401 | `COMPOSIO_API_KEY` is invalid or expired — rotate the key |
