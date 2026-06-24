# langfuse-loki-bridge

A lightweight polling bridge that pulls execution traces from [Langfuse](https://cloud.langfuse.com) and writes structured JSON logs to disk, where [Grafana Alloy](https://grafana.com/docs/alloy/) picks them up and ships them to **Grafana Loki** for visualization in Grafana dashboards.

---

## What It Does

- Polls the Langfuse API for new traces and LLM generation observations every 5 minutes (via cron)
- Writes one JSON log line per trace to `/var/log/langfuse/executions.log`
- Writes per-model token usage and cost summaries to `/var/log/langfuse/model_usage.log`
- Resolves organization names from a PostgreSQL datasource via the Grafana API
- Processes data in 1-hour windows to handle large backlogs safely
- Self-terminates after 210 seconds (hard timeout) to stay within the 5-minute cron cycle
- Uses a state file to track the last processed timestamp across runs

---

## Folder Structure

```
langfuse-loki-bridge/
├── langfuse_to_grafana.py   # Main bridge script
├── .env.example             # Environment variable template (copy to .env)
├── .gitignore               # Excludes .env from version control
├── requirements.txt         # Python dependencies
└── README.md                # This file
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
cd langfuse-loki-bridge
pip3 install -r requirements.txt --break-system-packages
```

### 2. Configure environment variables

```bash
cp .env.example .env
nano .env
```

Fill in your actual credentials in `.env`:

```env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

GRAFANA_URL=https://your-stack.grafana.net
GRAFANA_TOKEN=glsa_...
GRAFANA_DS_UID=your-datasource-uid
```

> **Never commit `.env` to git.** It is listed in `.gitignore`.

### 3. Create log directories

```bash
sudo mkdir -p /var/log/langfuse
sudo chown $USER:$USER /var/log/langfuse
```

### 4. Create the state file directory

```bash
mkdir -p ~/langfuse-setup
```

### 5. Run manually to test

```bash
python3 langfuse_to_grafana.py
```

Expected output:

```
[2026-06-23 13:00:00] Starting script
  Polling since: 2026-06-23T12:55:00+00:00
  Loaded 153 org names
--- Polling window: 2026-06-23T12:55:00+00:00 to 2026-06-23T13:00:00+00:00 ---
  Fetched 126/126 traces
  Fetched 126 traces in this window
  Writing logs...
  Logs written.
  Fetching model usage...
  Model usage fetched.
  Writing model logs...
  Model logs written.
  Saved poll time → 2026-06-23T13:00:00+00:00
[2026-06-23 13:01:05] Done
```

### 6. Set up cron (every 5 minutes)

```bash
crontab -e
```

Add this line:

```
*/5 * * * * flock -n /tmp/langfuse.lock -c 'cd /home/<your-user>/langfuse-loki-bridge && python3 langfuse_to_grafana.py' >> /var/log/langfuse/cron.log 2>&1
```

> `flock` ensures only one instance runs at a time — safe if a run takes longer than expected.

---

## Log Output Format

### executions.log (one line per trace)

```json
{
  "timestamp": "2026-06-23T13:00:01.000Z",
  "trace_id": "abc123",
  "name": "LangGraph",
  "agent_id": "LangGraph",
  "org_id": "uuid-here",
  "org_name": "Acme Corp",
  "user_id": "uuid-here",
  "status": "success",
  "latency_ms": 4500,
  "total_cost": 0.024614,
  "input_tokens": 0,
  "output_tokens": 0,
  "total_tokens": 0,
  "session_id": "session-uuid",
  "environment": "default",
  "level": "info"
}
```

### model_usage.log (per model, per polling window)

```json
{
  "timestamp": "2026-06-23T13:00:00+00:00",
  "model": "claude-sonnet-4-6",
  "input_tokens": 4408955,
  "output_tokens": 28221,
  "total_tokens": 4437176,
  "total_cost": 4.038439,
  "generation_count": 54,
  "level": "info"
}
```

---

## State File

The script tracks its progress in:

```
~/langfuse-setup/.last_poll_time
```

This file stores the ISO timestamp of the last successfully processed window. If the script is interrupted or times out, it resumes from this point on the next run.

To reset and start fresh from a specific time:

```bash
echo -n "2026-06-23T12:00:00+00:00" > ~/langfuse-setup/.last_poll_time
```

---

## Grafana Alloy Configuration

Alloy must be configured to tail the log files and ship them to Loki. Example pipeline:

```hcl
local.file_match "langfuse_logs" {
  path_targets = [{
    __path__ = "/var/log/langfuse/*.log"
  }]
}

loki.source.file "langfuse" {
  targets    = local.file_match.langfuse_logs.targets
  forward_to = [loki.write.default.receiver]
}
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Script times out every run | State file too far in the past — reset it to a recent timestamp |
| `[TIMEOUT]` in cron.log but state not updating | Cron and manual run overlapping — use `flock` in cron (already included) |
| `KeyError` on startup | `.env` file missing or a variable not set — check against `.env.example` |
| `422` from Langfuse API | Timestamp format issue — script handles this with retry logic |
| Org names show empty | Grafana datasource UID wrong or token lacks permission |
