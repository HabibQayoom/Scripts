#!/usr/bin/env python3
"""
AIOS Production Alert System — Loki Edition
- Polls Grafana Loki every 30s
- Each UNIQUE error alerts ONCE per run, never repeats (no summary spam)
- Counts every occurrence for the manual report
- Sends errors to #aios-logs-reporting, manual report to #credit-alerts
- UI at port 9002: review, comment, silence, send report, 24h/3day/since-restart views
"""

import json
import re
import time
import threading
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

import config as C

# ── Config (from .env via config.py) ──────────────────────────────────────────
GRAFANA_URL     = C.GRAFANA_URL
GRAFANA_TOKEN   = C.GRAFANA_TOKEN
LOKI_DS_ID      = C.LOKI_DS_ID
NAMESPACE       = C.NAMESPACE
CLUSTER         = C.CLUSTER

SLACK_BOT_TOKEN = C.SLACK_BOT_TOKEN
ALERT_CHANNEL   = C.ALERT_CHANNEL
REPORT_CHANNEL  = C.REPORT_CHANNEL

POLL_SECONDS    = C.POLL_SECONDS
UI_PORT         = C.UI_PORT

# Loki LogQL stream selector — cluster filter optional (label may be removed)
_cluster_sel = f'cluster="{CLUSTER}", ' if CLUSTER else ""
LOKI_QUERY = (
    "{" + _cluster_sel + f'namespace="{NAMESPACE}", '
    'container!~"alloy.*|grafana.*"' + "}"
    ' | detected_level="error"'
)

LOG_DIR         = C.DATA_DIR
LOG_FILE        = LOG_DIR / "aios-application-alerts.log"
STATE_FILE      = LOG_DIR / "aios-application-alerts.state"
SILENCE_FILE    = LOG_DIR / "silenced-groups.json"
COUNTS_FILE     = LOG_DIR / "daily-error-counts.json"
RESTART_FILE    = LOG_DIR / "view-restart.txt"
MAX_LOG_BYTES   = C.MAX_LOG_BYTES
MAX_LOG_DAYS    = C.MAX_LOG_DAYS
COUNTS_MAX_DAYS = C.COUNTS_MAX_DAYS
# ──────────────────────────────────────────────────────────────────────────────


# Keys already alerted this run — never alert the same error twice
_alerted: set = set()
_last_poll_time: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def kql_ts(iso: str) -> str:
    """Format timestamp for KQL datetime() — no Z, no microseconds."""
    ts = iso.replace("Z", "").replace("+00:00", "")
    return ts[:19]


def normalize(message: str) -> str:
    m = message
    m = re.sub(r"\x1b?\[[0-9;]*m", "", m)
    m = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*[+\-Z:0-9]*", "", m)
    m = re.sub(r"<\d+\.\d+\.\d+>", "<PID>", m)
    m = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?", "IP", m)
    m = re.sub(r"\[?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\]?", "*", m)
    m = re.sub(r"Step[0-9a-f]{8}[-0-9a-f]*", "Step*", m)
    m = re.sub(r"req_\w+", "req_*", m)
    m = re.sub(r"Task-\d+", "Task-*", m)
    m = re.sub(r"0x[0-9a-fA-F]+", "0x*", m)
    m = re.sub(r"\b\d{4,}\b", "N", m)
    lines = m.splitlines()
    return lines[0].strip()[:120] if lines else ''


def error_key(container: str, log_message: str) -> str:
    if not log_message or not log_message.strip():
        return f"{container}|empty"
    try:
        data    = json.loads(log_message)
        logger  = data.get("logger", "")
        message = data.get("message", "").strip()
        return f"{container}|{logger}|{normalize(message)}"
    except (json.JSONDecodeError, ValueError):
        return f"{container}|{normalize(log_message.strip())}"


def setup():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    with open(LOG_FILE, "a") as f:
        f.write(line)


def rotate_log():
    if not LOG_FILE.exists():
        return
    stat = LOG_FILE.stat()
    if stat.st_size > MAX_LOG_BYTES:
        LOG_FILE.unlink()
        log("Log deleted (size)")
    elif (time.time() - stat.st_mtime) / 86400 > MAX_LOG_DAYS:
        LOG_FILE.unlink()
        log("Log deleted (age)")


def load_last_ts() -> str:
    if STATE_FILE.exists():
        try:
            val = STATE_FILE.read_text().strip()
            if val:
                return val
        except Exception:
            pass
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def save_last_ts(ts_iso: str):
    STATE_FILE.write_text(ts_iso)


# ── Silence ───────────────────────────────────────────────────────────────────
def load_silenced() -> dict:
    if SILENCE_FILE.exists():
        try:
            return json.loads(SILENCE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_silenced(data: dict):
    SILENCE_FILE.write_text(json.dumps(data, indent=2))


def is_silenced(key: str) -> bool:
    silenced = load_silenced()
    if key not in silenced:
        return False
    expires = silenced[key].get("expires")
    if expires and time.time() > expires:
        del silenced[key]
        save_silenced(silenced)
        return False
    return True


# ── Counts ────────────────────────────────────────────────────────────────────
def load_counts() -> dict:
    if COUNTS_FILE.exists():
        try:
            return json.loads(COUNTS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_counts(data: dict):
    COUNTS_FILE.write_text(json.dumps(data, indent=2))


def purge_old_counts():
    counts  = load_counts()
    if not counts:
        return
    cutoff  = datetime.now(timezone.utc) - timedelta(days=COUNTS_MAX_DAYS)
    removed = 0
    kept    = {}
    for k, v in counts.items():
        try:
            last = datetime.fromisoformat(v.get("last_seen", v.get("first_seen", "")).replace("Z", "+00:00"))
            if last >= cutoff:
                kept[k] = v
            else:
                removed += 1
        except Exception:
            kept[k] = v
    if removed:
        save_counts(kept)
        log(f"Purged {removed} error entries older than {COUNTS_MAX_DAYS} days")


def increment_count(key: str, msg: str, raw_line: str, pod: str):
    counts = load_counts()
    nowiso = now_iso()
    if key not in counts:
        counts[key] = {
            "message":    msg,
            "count":      0,
            "first_seen": nowiso,
            "comment":    "",
            "samples":    [],
        }
    counts[key]["count"]    += 1
    counts[key]["last_seen"] = nowiso
    sample = {"ts": nowiso, "pod": pod, "raw": raw_line[:500]}
    counts[key].setdefault("samples", []).append(sample)
    counts[key]["samples"] = counts[key]["samples"][-20:]
    save_counts(counts)


def filter_counts_by_age(counts: dict, days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out    = {}
    for k, v in counts.items():
        try:
            last = datetime.fromisoformat(v.get("last_seen", v.get("first_seen", "")).replace("Z", "+00:00"))
            if last >= cutoff:
                out[k] = v
        except Exception:
            out[k] = v
    return out


def load_restart_ts() -> str:
    if RESTART_FILE.exists():
        try:
            return RESTART_FILE.read_text().strip()
        except Exception:
            pass
    return ""


def save_restart_ts():
    ts = now_iso()
    RESTART_FILE.write_text(ts)
    return ts


def filter_counts_since(counts: dict, since_iso: str) -> dict:
    if not since_iso:
        return filter_counts_by_age(counts, 1)
    try:
        cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except Exception:
        return filter_counts_by_age(counts, 1)
    out = {}
    for k, v in counts.items():
        try:
            last = datetime.fromisoformat(v.get("last_seen", v.get("first_seen", "")).replace("Z", "+00:00"))
            if last >= cutoff:
                out[k] = v
        except Exception:
            pass
    return out


# ── Azure Monitor ─────────────────────────────────────────────────────────────
def query_loki(start_ns: int, end_ns: int) -> list:
    """Query Loki. Returns list of (ts_iso, container, pod, log_message)."""
    global _last_poll_time
    try:
        r = requests.get(
            f"{GRAFANA_URL}/api/datasources/proxy/{LOKI_DS_ID}/loki/api/v1/query_range",
            headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"},
            params={"query": LOKI_QUERY, "start": start_ns, "end": end_ns,
                    "limit": 200, "direction": "forward"},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
    except Exception as e:
        log(f"ERROR Loki query failed: {e}")
        return []

    entries = []
    for stream in results:
        labels = stream.get("stream", {})
        container = labels.get("container", "") or "unknown"
        pod       = labels.get("pod", "") or ""
        for ts_str, line in stream.get("values", []):
            # Loki ts is ns-epoch string
            try:
                ts_iso = datetime.fromtimestamp(int(ts_str) / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            except Exception:
                ts_iso = now_iso()
            entries.append((int(ts_str), ts_iso, container, pod, line))

    entries.sort(key=lambda x: x[0])
    _last_poll_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    log(f"POLL | {len(entries)} entries")
    # strip the sort key, return (ts_iso, container, pod, line)
    return [(e[1], e[2], e[3], e[4]) for e in entries]


def post_slack(channel: str, text: str):
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
            json={"channel": channel, "text": text, "mrkdwn": True},
            timeout=10,
        )
        if not r.json().get("ok"):
            log(f"WARN Slack error: {r.json().get('error')}")
    except Exception as e:
        log(f"ERROR Slack post failed: {e}")


def build_message(container: str, pod: str, log_message: str) -> str:
    try:
        data    = json.loads(log_message)
        logger  = data.get("logger", "")
        message = data.get("message", "").strip()
        exc     = data.get("exc_info", "")
        text    = f"*[{container}]*"
        if logger:
            text += f" `{logger}`"
        text += f"\n{message}"
        if exc:
            last_line = [l.strip() for l in exc.strip().splitlines() if l.strip()][-1]
            text += f"\n```{last_line}```"
    except (json.JSONDecodeError, ValueError):
        text = f"*[{container}]*\n{log_message.strip()}"
    if pod:
        text += f"\n_pod: {pod}_"
    return text


def process_entry(container: str, pod: str, log_message: str):
    if not log_message or not log_message.strip():
        return
    key = error_key(container, log_message)
    msg = build_message(container, pod, log_message)

    # Always count for the report (clean message, no pod line)
    clean_msg = "\n".join(l for l in msg.split("\n") if not l.startswith("_pod:"))
    increment_count(key, clean_msg, log_message, pod)

    # Skip silenced errors
    if is_silenced(key):
        return

    # Alert ONCE per run for each unique error — never repeat / no spam
    if key in _alerted:
        return

    post_slack(ALERT_CHANNEL, msg)
    log(f"SENT {msg[:100]}")
    _alerted.add(key)


# ── Report ────────────────────────────────────────────────────────────────────
def build_report_text(counts: dict, selected_keys: list) -> str:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(counts[k]["count"] for k in selected_keys if k in counts)
    lines = [
        f"*AIOS Error Report — {now}*",
        f"Unique error types: *{len(selected_keys)}* | Total occurrences: *{total}*",
        ""
    ]
    for i, key in enumerate(selected_keys, 1):
        e         = counts[key]
        msg_short = e["message"][:150].replace("\n", " ")
        comment   = e.get("comment", "").strip()
        line      = f"*{i}.* `{e['count']}x` — {msg_short}"
        if comment:
            line += f"\n   _comment: {comment}_"
        lines.append(line)
    return "\n".join(lines)


def send_report(selected_keys: list, counts: dict):
    text = build_report_text(counts, selected_keys)
    post_slack(REPORT_CHANNEL, text)
    log(f"Manual report sent ({len(selected_keys)} errors)")


# ── UI ────────────────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:1320px;margin:0 auto;padding:28px 22px;background:#0a0a0f;color:#e4e4e7}
h1{color:#a78bfa;margin:0 0 2px;font-size:26px;font-weight:700}
.sub{color:#71717a;margin-bottom:22px;font-size:13px}
h2{color:#c4b5fd;font-size:17px;margin:30px 0 6px;display:flex;align-items:center;gap:8px}
.tabs{display:flex;gap:6px;margin:18px 0 6px;border-bottom:1px solid #27272a}
.tab{padding:9px 18px;background:none;border:none;color:#71717a;cursor:pointer;font-size:14px;font-weight:600;border-bottom:2px solid transparent}
.tab.active{color:#a78bfa;border-bottom-color:#a78bfa}
.panel{display:none}.panel.active{display:block}
.badge{display:inline-block;color:#fff;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:700;background:#dc2626;min-width:34px;text-align:center}
.badge.s{background:#16a34a}.badge.w{background:#ca8a04}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
th{background:#14141c;padding:9px 11px;text-align:left;color:#a78bfa;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.3px}
td{padding:9px 11px;border-bottom:1px solid #18181f;vertical-align:top}
tr:hover td{background:#0f0f17}
.msg{font-family:'SF Mono',ui-monospace,monospace;font-size:12px;word-break:break-word;max-width:440px;color:#d4d4d8;line-height:1.5}
.btn{padding:5px 12px;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;margin:1px;transition:opacity .15s}
.btn:hover{opacity:.85}
.bs{background:#7c3aed;color:#fff}.bd{background:#3f3f46;color:#fff}
.bu{background:#16a34a;color:#fff}.brem{background:#7f1d1d;color:#fff}
.bsend{background:#0891b2;color:#fff;padding:11px 24px;font-size:14px}
.bexp{background:#27272a;color:#a78bfa;font-size:11px;padding:3px 9px}
input,select,textarea{background:#16161e;border:1px solid #2a2a35;color:#e4e4e7;padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:#7c3aed}
textarea{width:100%;resize:vertical}
.card{background:#0e0e15;border:1px solid #1e1e28;border-radius:10px;padding:16px 18px;margin-bottom:10px}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;font-size:14px;font-weight:600;z-index:999;box-shadow:0 8px 24px rgba(0,0,0,.4);opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}.toast.ok{background:#14532d;color:#bbf7d0}.toast.err{background:#7f1d1d;color:#fecaca}
.tag{font-size:10px;background:#1e1b4b;color:#a78bfa;padding:2px 7px;border-radius:4px;margin-left:5px}
.samples{background:#08080d;border-left:2px solid #3f3f46;margin:4px 0 4px 20px;padding:8px 12px;font-family:monospace;font-size:11px;color:#a1a1aa;display:none}
.samples.open{display:block}
.samples div{padding:3px 0;border-bottom:1px solid #161620;word-break:break-all}
.silence-inline{display:flex;gap:4px;align-items:center;margin-top:4px}
.silence-inline input{width:50px;padding:4px 6px;font-size:11px}
.silence-inline select{padding:4px 6px;font-size:11px}
.stat{display:inline-flex;flex-direction:column;background:#0e0e15;border:1px solid #1e1e28;border-radius:10px;padding:14px 22px;margin-right:12px}
.stat .n{font-size:28px;font-weight:700;color:#f87171}
.stat .l{font-size:12px;color:#71717a;margin-top:2px}
.source-badge{font-size:10px;background:#0c4a6e;color:#38bdf8;padding:2px 8px;border-radius:4px;margin-left:8px;font-weight:600}
"""

JS = """
function toast(msg,ok){let t=document.getElementById('toast');t.textContent=msg;t.className='toast '+(ok?'ok':'err')+' show';setTimeout(()=>t.className='toast',2500);}
async function post(url,data){let body=new URLSearchParams(data).toString();let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});return r.json();}
async function silence(key,unit,val){let r=await post('/api/silence',{key,duration_unit:unit,duration_value:val});if(r.ok){toast('Silenced: '+r.label,true);markSilenced(key);}else toast('Failed',false);}
async function silenceCustom(key,kid){let unit=document.getElementById('u_'+kid);let val=document.getElementById('v_'+kid);if(!unit||!val){toast('UI error',false);return;}silence(key,unit.value,val.value);}
async function unsilence(key){let r=await post('/api/unsilence',{key});if(r.ok){toast('Unsilenced',true);setTimeout(()=>location.reload(),600);}else toast('Failed',false);}
async function delcount(key){if(!confirm('Remove?'))return;let r=await post('/api/delete-count',{key});if(r.ok){toast('Removed',true);document.querySelectorAll('[data-key="'+CSS.escape(key)+'"]').forEach(e=>e.remove());}else toast('Failed',false);}
function markSilenced(key){document.querySelectorAll('[data-key="'+CSS.escape(key)+'"] .badge').forEach(b=>b.classList.add('s'));}
function toggleSamples(id){let el=document.getElementById('s_'+id);el.classList.toggle('open');}
async function restartView(){if(!confirm('Reset view to now?'))return;let r=await post('/api/restart-view',{});if(r.ok){toast('View reset',true);setTimeout(()=>{switchTab('since');location.reload();},600);}else toast('Failed',false);}
function switchTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));document.getElementById('tab_'+name).classList.add('active');document.getElementById('panel_'+name).classList.add('active');}
"""

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AIOS Alert Manager</title>
<style>{css}</style></head><body>
<div id="toast" class="toast"></div>
<h1>AIOS Alert Manager <span class="source-badge">Loki</span></h1>
<p class="sub">Review &amp; send manual reports | Silence noisy errors | Last poll: {last_poll}</p>
<div style="margin-bottom:18px">
  <div class="stat"><span class="n">{total_24h}</span><span class="l">Errors (last 24h)</span></div>
  <div class="stat"><span class="n">{types_24h}</span><span class="l">Unique types (24h)</span></div>
  <div class="stat"><span class="n">{total_3d}</span><span class="l">Errors (last 3 days)</span></div>
  <div class="stat"><span class="n">{silenced_n}</span><span class="l">Silenced</span></div>
</div>
<h2>Review &amp; Send Report</h2>
<p style="color:#71717a;font-size:13px">Uncheck rows to exclude. Only checked rows sent to #credit-alerts.</p>
<form method="POST" action="/send-report">
{report_table}
<div style="margin-top:14px">
  <button class="btn bsend" type="submit" onclick="return confirm('Send report to #credit-alerts?')">Send Report to #credit-alerts</button>
</div>
</form>
<h2>Error Counts</h2>
<div class="tabs">
  <button class="tab active" id="tab_24h" onclick="switchTab('24h')">Last 24 Hours</button>
  <button class="tab" id="tab_since" onclick="switchTab('since')">Since Restart</button>
  <button class="tab" id="tab_3d" onclick="switchTab('3d')">Last 3 Days</button>
  <button class="tab" id="tab_sil" onclick="switchTab('sil')">Active Silences ({silenced_n})</button>
</div>
<div class="panel active" id="panel_24h">{table_24h}</div>
<div class="panel" id="panel_since">
  <div class="card" style="display:flex;align-items:center;justify-content:space-between">
    <div>
      <div style="font-size:13px;color:#a1a1aa">Showing errors since: <b style="color:#a78bfa">{restart_label}</b></div>
      <div style="font-size:12px;color:#52525b;margin-top:2px">Press Restart to reset this view to now.</div>
    </div>
    <button type="button" class="btn bsend" style="background:#dc2626" onclick="restartView()">Restart View</button>
  </div>
  {table_since}
</div>
<div class="panel" id="panel_3d">{table_3d}</div>
<div class="panel" id="panel_sil">{silences}</div>
<script>{js}</script>
</body></html>"""


def render_error_table(counts: dict) -> str:
    if not counts:
        return "<p style='color:#52525b'>No errors in this period.</p>"
    sorted_counts = sorted(counts.items(), key=lambda x: x[1]["count"], reverse=True)
    rows = ""
    for key, e in sorted_counts:
        kid       = abs(hash(key)) % (10**8)
        sil       = is_silenced(key)
        badge_cls = "s" if sil else ("w" if e["count"] >= 10 else "")
        msg       = e["message"][:240].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n"," ")
        key_attr  = key.replace('"',"&quot;")
        samples   = e.get("samples", [])
        sample_html = ""
        if samples:
            for s in reversed(samples):
                raw = s.get("raw","")[:300].replace("<","&lt;").replace(">","&gt;")
                pod = s.get("pod","")
                ts  = s.get("ts","")[:19]
                sample_html += f"<div><span style='color:#7c3aed'>{ts}</span> <span style='color:#16a34a'>{pod}</span><br>{raw}</div>"
        rows += f"""<tr data-key="{key_attr}">
          <td><span class="badge {badge_cls}">{e['count']}x</span></td>
          <td class="msg">{msg}
            {'<button type="button" class="btn bexp" onclick="toggleSamples('+str(kid)+')">show '+str(len(samples))+' occurrences</button>' if samples else ''}
          </td>
          <td style="width:90px">{e.get('first_seen','')[:10]}<br><span style="color:#52525b">{e.get('last_seen','')[11:16]}</span></td>
          <td style="width:230px">
            <div class="silence-inline">
              <input type="number" id="v_{kid}" value="1" min="1" style="width:50px">
              <select id="u_{kid}">
                <option value="hours">hrs</option>
                <option value="days">days</option>
                <option value="forever">forever</option>
              </select>
              <button type="button" class="btn bs" onclick="silenceCustom('{js_key(key)}',{kid})">Silence</button>
            </div>
            <div style="margin-top:3px">
              <button type="button" class="btn bd" onclick="silence('{js_key(key)}','hours','1')">1h</button>
              <button type="button" class="btn bd" onclick="silence('{js_key(key)}','hours','24')">24h</button>
              <button type="button" class="btn bd" onclick="silence('{js_key(key)}','days','7')">7d</button>
              <button type="button" class="btn brem" onclick="delcount('{js_key(key)}')">X</button>
            </div>
          </td>
        </tr>
        <tr><td colspan="4" style="padding:0;border:none">
          <div class="samples" id="s_{kid}">{sample_html or 'No samples stored'}</div>
        </td></tr>"""
    return f"<table><tr><th>Count</th><th>Error Message</th><th>Seen</th><th>Actions</th></tr>{rows}</table>"


def js_key(key):
    return key.replace("\\","\\\\").replace("'","\\'").replace('"','\\"')


def render(counts=None):
    global _last_poll_time
    if counts is None:
        counts = load_counts()
    silenced     = load_silenced()
    counts_24h   = filter_counts_by_age(counts, 1)
    counts_3d    = filter_counts_by_age(counts, 3)
    restart_ts   = load_restart_ts()
    counts_since = filter_counts_since(counts, restart_ts)

    if restart_ts:
        try:
            restart_label = datetime.fromisoformat(restart_ts.replace("Z","+00:00")).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            restart_label = restart_ts
    else:
        restart_label = "(not set — showing last 24h)"

    total_24h = sum(e["count"] for e in counts_24h.values())
    total_3d  = sum(e["count"] for e in counts_3d.values())
    types_24h = len(counts_24h)

    if counts_24h:
        sorted_counts = sorted(counts_24h.items(), key=lambda x: x[1]["count"], reverse=True)
        rows = ""
        for key, e in sorted_counts:
            msg      = e["message"][:200].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n"," ")
            comm     = e.get("comment","").replace('"',"&quot;")
            sil      = " <span class='tag'>silenced</span>" if is_silenced(key) else ""
            key_attr = key.replace('"',"&quot;")
            rows += f"""<tr>
              <td style="width:36px;text-align:center"><input type="checkbox" name="include" value="{key_attr}" checked></td>
              <td><span class="badge {'s' if is_silenced(key) else ''}">{e['count']}x</span>{sil}</td>
              <td class="msg">{msg}</td>
              <td style="width:210px"><textarea name="comment_{key_attr}" rows="2" placeholder="comment...">{comm}</textarea></td>
            </tr>"""
        report_table = f"""<table>
          <tr><th><input type="checkbox" checked onclick="document.querySelectorAll('input[name=include]').forEach(c=>c.checked=this.checked)"></th>
          <th>Count</th><th>Error</th><th>Comment</th></tr>{rows}</table>"""
    else:
        report_table = "<p style='color:#52525b'>No errors recorded yet.</p>"

    if silenced:
        srows = "".join(f"""<tr data-key="{k.replace('"','&quot;')}">
          <td class="msg">{k[:90]}</td><td>{v.get('reason','—')}</td><td>{v.get('duration','—')}</td>
          <td>{v.get('silenced_at','')[:19]}</td>
          <td>{'Forever' if not v.get('expires') else datetime.fromtimestamp(v['expires']).strftime('%Y-%m-%d %H:%M')}</td>
          <td><button type="button" class="btn bu" onclick="unsilence('{js_key(k)}')">Unsilence</button></td>
        </tr>""" for k, v in silenced.items())
        silences = f"<table><tr><th>Key</th><th>Reason</th><th>For</th><th>Silenced At</th><th>Expires</th><th></th></tr>{srows}</table>"
    else:
        silences = "<p style='color:#52525b'>No active silences.</p>"

    return HTML.format(
        css=CSS, js=JS,
        total_24h=total_24h, types_24h=types_24h, total_3d=total_3d,
        silenced_n=len(silenced),
        report_table=report_table,
        table_24h=render_error_table(counts_24h),
        table_since=render_error_table(counts_since),
        table_3d=render_error_table(counts_3d),
        silences=silences,
        restart_label=restart_label,
        last_poll=_last_poll_time or "not yet",
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, content, ctype="text/html"):
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, obj):
        self._send(json.dumps(obj), "application/json")

    def do_GET(self):
        self._send(render())

    def do_POST(self):
        body   = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        params = parse_qs(body, keep_blank_values=True)
        p      = lambda k: params.get(k, [""])[0].strip()

        if self.path == "/api/silence":
            key  = p("key"); unit = p("duration_unit"); val = p("duration_value")
            if not key:
                return self._json({"ok": False})
            if unit == "forever":
                dur, label = 0, "Forever"
            elif unit == "days":
                dur = int(val or 1) * 86400; label = f"{val}d"
            else:
                dur = int(val or 1) * 3600; label = f"{val}h"
            silenced = load_silenced()
            silenced[key] = {"reason": "Manual silence", "silenced_at": now_iso()[:19],
                             "expires": time.time() + dur if dur > 0 else None, "duration": label}
            save_silenced(silenced)
            log(f"SILENCED via UI ({label}) {key[:60]}")
            return self._json({"ok": True, "label": label})

        if self.path == "/api/unsilence":
            key = p("key"); silenced = load_silenced()
            if key in silenced:
                del silenced[key]; save_silenced(silenced)
                return self._json({"ok": True})
            return self._json({"ok": False})

        if self.path == "/api/restart-view":
            ts = save_restart_ts(); log(f"View restarted at {ts}")
            return self._json({"ok": True, "ts": ts})

        if self.path == "/api/delete-count":
            key = p("key"); counts = load_counts()
            if key in counts:
                del counts[key]; save_counts(counts)
                return self._json({"ok": True})
            return self._json({"ok": False})

        if self.path == "/send-report":
            counts   = load_counts()
            included = params.get("include", [])
            if not included:
                return self._send(render())
            for key in counts:
                ckey = f"comment_{key}"
                if ckey in params:
                    counts[key]["comment"] = params[ckey][0].strip()
            save_counts(counts)
            send_report(included, counts)
            return self._send(render())

        self.send_response(404)
        self.end_headers()


def main():
    setup()
    rotate_log()
    log(f"Started | Source=Loki | poll={POLL_SECONDS}s | UI=:{UI_PORT}")

    server = HTTPServer(("0.0.0.0", UI_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"UI started at http://0.0.0.0:{UI_PORT}")

    # Loki uses nanosecond cursor
    last_ns = int(time.time() * 1e9)
    poll_count = 0

    while True:
        time.sleep(POLL_SECONDS)
        poll_count += 1

        if poll_count % 120 == 0:
            rotate_log()
            purge_old_counts()

        now_ns  = int(time.time() * 1e9)
        entries = query_loki(last_ns + 1, now_ns)

        for ts_iso, container, pod, log_message in entries:
            process_entry(container, pod, log_message)
            time.sleep(0.1)

        last_ns = now_ns


if __name__ == "__main__":
    main()
