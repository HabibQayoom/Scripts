#!/usr/bin/env python3

import os
from dotenv import load_dotenv
import json
import time
import requests
import threading
from datetime import datetime, timezone, timedelta

load_dotenv()

"""
Langfuse → Grafana Loki bridge
Polls Langfuse traces and writes structured logs to /var/log/langfuse/executions.log
Loki picks these up via Alloy and makes them available in Grafana.

Hard limit: script will self-terminate after 210 seconds (3.5 minutes).
Cron: every 5 minutes — gives ~1.5 min buffer after hard stop.
"""

LANGFUSE_PUBLIC_KEY = os.environ["LANGFUSE_PUBLIC_KEY"]
LANGFUSE_SECRET_KEY = os.environ["LANGFUSE_SECRET_KEY"]
LANGFUSE_BASE_URL   = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

GRAFANA_URL      = os.environ["GRAFANA_URL"]
GRAFANA_TOKEN    = os.environ["GRAFANA_TOKEN"]
GRAFANA_DS_UID   = os.environ["GRAFANA_DS_UID"]

STATE_FILE = os.path.expanduser("~/langfuse-setup/.last_poll_time")
LOG_FILE   = "/var/log/langfuse/executions.log"

HARD_TIMEOUT_SECONDS = 210   # 3.5 minutes — cron is every 5 min so 1.5 min buffer
MAX_TRACES           = 5000  # safety cap per run
MAX_RETRIES          = 3
PAGE_SLEEP           = 0.1   # 0.1 second between pages to avoid rate limiting

_org_name_cache: dict = {}

def _timeout_handler():
    print("\n[TIMEOUT] Hard limit reached — exiting safely.")
    os._exit(1)  # force exit (works cross-platform, safer than sys.exit in threads)


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get_with_retry(url, auth, params, label):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, auth=auth, params=params, timeout=30)

            if r.status_code == 429:
                wait = 30 * attempt
                print(f"  {label} rate limited (429) — waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue

            if r.status_code in (502, 503, 504):
                wait = 15 * attempt
                print(f"  {label} server error ({r.status_code}) — waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                print(f"  {label} unexpected status {r.status_code}: {r.text}", flush=True)
                time.sleep(10)
                continue

            return r.json()

        except Exception as e:
            print(f"  {label} error: {e}", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(10)

    print(f"  {label} — max retries exhausted", flush=True)
    return None


# ── ORG NAMES ──────────────────────────────────────────────────────────────────

def load_org_names():
    global _org_name_cache
    if _org_name_cache:
        return _org_name_cache

    try:
        r = requests.post(
            f"{GRAFANA_URL}/api/ds/query",
            headers={
                "Authorization": f"Bearer {GRAFANA_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "queries": [{
                    "refId": "A",
                    "datasource": {"type": "grafana-azure-monitor-datasource", "uid": GRAFANA_DS_UID},
                    "rawSql": "SELECT id::text, name FROM organizations WHERE is_active = true",
                    "format": "table"
                }],
                "from": "now-5m",
                "to": "now"
            },
            timeout=120
        )

        frames = r.json().get("results", {}).get("A", {}).get("frames", [])
        if frames and frames[0]["data"]["values"]:
            ids = frames[0]["data"]["values"][0]
            names = frames[0]["data"]["values"][1]
            _org_name_cache = dict(zip(ids, names))
            print(f"  Loaded {len(_org_name_cache)} org names")

    except Exception as e:
        print(f"  Org lookup failed: {e}")

    return _org_name_cache


# ── STATE FILE ─────────────────────────────────────────────────────────────────

def get_last_poll_time():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except:
        return "2026-05-08T00:00:00+00:00"


def save_poll_time(ts):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(ts)


# ── FETCH TRACES ───────────────────────────────────────────────────────────────

def fetch_traces(from_ts, to_ts):
    traces, page = [], 1

    while True:
        d = _get_with_retry(
            url=f"{LANGFUSE_BASE_URL}/api/public/traces",
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            params={"limit": 100, "page": page, "fromTimestamp": from_ts, "toTimestamp": to_ts},
            label=f"Page {page}"
        )

        if d is None:
            break

        batch = d.get("data", [])
        if not batch:
            break

        traces.extend(batch)

        total = d.get("meta", {}).get("totalItems", 0)
        print(f"  Fetched {len(traces)}/{total} traces", end="\r")

        if len(traces) >= MAX_TRACES:
            print("\n  MAX_TRACES reached")
            break

        if len(traces) >= total:
            break

        page += 1
        time.sleep(PAGE_SLEEP)

    return traces


# ── FETCH MODEL USAGE ──────────────────────────────────────────────────────────

def fetch_model_usage(from_ts, to_ts):
    usage = {}
    page = 1

    while True:
        d = _get_with_retry(
            url=f"{LANGFUSE_BASE_URL}/api/public/observations",
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            params={
                "limit": 100,
                "page": page,
                "fromStartTime": from_ts,
                "toStartTime": to_ts,
                "type": "GENERATION"
            },
            label=f"Model usage page {page}"
        )

        if d is None:
            break

        batch = d.get("data", [])
        if not batch:
            break

        for obs in batch:
            model = obs.get("model") or "unknown"
            cost = obs.get("calculatedTotalCost") or 0
            inp = obs.get("promptTokens") or 0
            out = obs.get("completionTokens") or 0

            if model not in usage:
                usage[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_cost": 0,
                    "count": 0
                }

            usage[model]["input_tokens"] += inp
            usage[model]["output_tokens"] += out
            usage[model]["total_cost"] += cost
            usage[model]["count"] += 1

        total = d.get("meta", {}).get("totalItems", 0)
        fetched = (page - 1) * 50 + len(batch)

        if fetched >= total:
            break

        page += 1
        time.sleep(PAGE_SLEEP)

    return usage


# ── HELPERS ────────────────────────────────────────────────────────────────────

def parse_iso(ts_str):
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    return datetime.fromisoformat(ts_str)


def extract_org_id(trace):
    user_id = trace.get("userId") or ""
    if user_id.startswith("org-") and "-user-" in user_id:
        return user_id[4:].split("-user-")[0]
    return ""


def extract_real_user_id(trace):
    user_id = trace.get("userId") or ""
    if "-user-" in user_id:
        return user_id.split("-user-")[-1]
    return user_id


def get_trace_name(trace):
    name = trace.get("name") or ""
    if len(name) == 36 and name.count("-") == 4:
        return "aios_agent"
    return name or "unknown"


def get_status(trace):
    latency = trace.get("latency") or 0
    return "warning" if latency > 600 else "success"


# ── LOG WRITERS ────────────────────────────────────────────────────────────────

MODEL_LOG_FILE = "/var/log/langfuse/model_usage.log"


def write_model_logs(usage, timestamp):
    os.makedirs(os.path.dirname(MODEL_LOG_FILE), exist_ok=True)

    with open(MODEL_LOG_FILE, "a") as f:
        for model, stats in usage.items():
            if not model or model == "unknown":
                continue

            entry = {
                "timestamp": timestamp,
                "model": model,
                "input_tokens": stats["input_tokens"],
                "output_tokens": stats["output_tokens"],
                "total_tokens": stats["input_tokens"] + stats["output_tokens"],
                "total_cost": round(stats["total_cost"], 6),
                "generation_count": stats["count"],
                "level": "info"
            }

            f.write(json.dumps(entry) + "\n")


def write_logs(traces, org_names=None):
    org_names = org_names or {}

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    with open(LOG_FILE, "a") as f:
        for t in traces:
            name = get_trace_name(t)
            cost = t.get("totalCost") or 0
            latency = t.get("latency") or 0

            org_id = extract_org_id(t)
            user_id = extract_real_user_id(t)

            entry = {
                "timestamp": t.get("timestamp", ""),
                "trace_id": t.get("id", ""),
                "name": name,
                "org_id": org_id,
                "org_name": org_names.get(org_id, ""),
                "user_id": user_id,
                "status": get_status(t),
                "latency_ms": round(latency * 1000),
                "total_cost": round(cost, 6),
                "input_tokens": t.get("promptTokens") or 0,
                "output_tokens": t.get("completionTokens") or 0,
                "total_tokens": t.get("totalTokens") or 0,
                "session_id": t.get("sessionId") or "",
                "environment": t.get("environment") or "default",
                "level": "info"
            }

            f.write(json.dumps(entry) + "\n")


# ── MAIN (WINDOWS SAFE) ────────────────────────────────────────────────────────

def main():
    timer = threading.Timer(HARD_TIMEOUT_SECONDS, _timeout_handler)
    timer.start()

    try:
        start = datetime.now()
        print(f"[{start}] Starting script")

        last = get_last_poll_time()
        print(f"  Polling since: {last}")

        org_names = load_org_names()

        # Parse start timestamp
        current_start = parse_iso(last)
        now = datetime.now(timezone.utc)

        # Buffer of 1 minute to avoid catching incomplete traces
        query_limit = now - timedelta(minutes=1)

        if current_start >= query_limit:
            print("  No new timeframe to poll (already up to date).")
            return

        # Define window size: 1 hour
        window_size = timedelta(hours=1)

        while current_start < query_limit:
            window_end = min(current_start + window_size, query_limit)

            from_ts = current_start.isoformat()
            to_ts = window_end.isoformat()

            print(f"\n--- Polling window: {from_ts} to {to_ts} ---")

            traces = fetch_traces(from_ts, to_ts)
            print(f"\n  Fetched {len(traces)} traces in this window")

            if traces:
                print("  Writing logs...", flush=True)
                write_logs(traces, org_names)
                print("  Logs written.", flush=True)

                # Fetch and write model usage for this window
                print("  Fetching model usage...", flush=True)
                usage = fetch_model_usage(from_ts, to_ts)
                print("  Model usage fetched.", flush=True)
                if usage:
                    print("  Writing model logs...", flush=True)
                    write_model_logs(usage, to_ts)
                    print("  Model logs written.", flush=True)

            # Save poll time after each window (progress is preserved even on timeout)
            save_poll_time(to_ts)
            print(f"  Saved poll time → {to_ts}")

            current_start = window_end

            # Brief pause between windows to be polite
            time.sleep(0.5)

        print(f"[{datetime.now()}] Done")

    finally:
        timer.cancel()


if __name__ == "__main__":
    main()
