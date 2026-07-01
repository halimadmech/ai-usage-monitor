"""
AI Usage Monitor
================
A compact desktop widget showing Claude Code and Codex CLI usage in one window:
live limit bars per provider plus tokens used for Today, Yesterday, and the
Last 30 Days.

Data sources (all local / your own account):
- Codex limits + tokens : read from Codex CLI's local logs.
- Claude limits (%)      : read from Claude Code's own usage endpoint using the
                          OAuth token that `claude` stores after you log in.
                          This is account-wide: it covers ALL Claude usage
                          (chat, Cowork, Claude Code, CLI), not just the CLI.
- Claude tokens         : read from Claude Code's local session logs (this PC
                          only; Claude chat usage is never logged locally).

Nothing is sent anywhere except your own authenticated request to Anthropic's
usage endpoint, exactly as Claude Code itself does.

Run:           python usage_monitor.py
Build:         see build_exe.bat (standalone AIUsage.exe)
Diagnose:      AIUsage.exe --test-claude   (writes a report you can read)
"""

import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# CONFIG  --  edit if you like
# --------------------------------------------------------------------------

REFRESH_SECONDS = 15          # how often the window re-reads local data
PREFERRED_PORT = 8765

WINDOW_WIDTH = 380            # snug around the card (the card fills this minus a small gap)
WINDOW_HEIGHT = 700           # fallback only; at launch this is set from WINDOW_FRACTION
WINDOW_FRACTION = 0.667       # widget height as a fraction of the screen height (~2/3)
WINDOW_MIN_HEIGHT = 300
WINDOW_MAX_HEIGHT = 2000
ALWAYS_ON_TOP = False         # True pins the window above others, widget-style

# Claude usage endpoint (the same one Claude Code uses). The User-Agent header
# is REQUIRED; without it the endpoint hard rate-limits. Poll no faster than
# ~180s. Edit CLAUDE_UA if a future Claude Code version rejects this one.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_UA = "claude-code/2.1.114"
CLAUDE_POLL_SECONDS = 300
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"

CLAUDE_LOG_DIR = Path.home() / ".claude" / "projects"
CODEX_LOG_DIRS = [Path.home() / ".codex" / "sessions", Path.home() / ".codex"]
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
# Codex limits can be read LIVE (no model call, no quota) by driving the official
# `codex app-server`'s `account/rateLimits/read` RPC. Polled gently; falls back to
# the last log snapshot if the codex binary isn't installed.
CODEX_POLL_SECONDS = 120
STATE_FILE = Path.home() / ".usage_monitor_state.json"  # welcome marker only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = value / 1000.0 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_reset(seconds):
    if seconds is None:
        return None
    try:
        s = int(seconds)
    except Exception:
        return None
    if s < 0:
        s = 0
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def fmt_ago(seconds):
    """'as of' phrasing for a past timestamp, e.g. '5m ago', '3d ago'."""
    if seconds is None:
        return None
    try:
        s = max(0, int(seconds))
    except Exception:
        return None
    if s < 60:
        return "just now"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d > 0:
        return f"{d}d ago"
    if h > 0:
        return f"{h}h ago"
    return f"{m}m ago"


# --------------------------------------------------------------------------
# local log parsers (token rows; Codex limits)
# --------------------------------------------------------------------------

def parse_claude():
    events = []
    if not CLAUDE_LOG_DIR.exists():
        return events
    seen = set()
    for path in CLAUDE_LOG_DIR.rglob("*.jsonl"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    msg = row.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    key = (msg.get("id"), row.get("requestId"))
                    if key != (None, None) and key in seen:
                        continue
                    seen.add(key)
                    inp = int(usage.get("input_tokens", 0) or 0)
                    out = int(usage.get("output_tokens", 0) or 0)
                    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                    if inp == out == cw == cr == 0:
                        continue
                    events.append({
                        "ts": parse_ts(row.get("timestamp")),
                        "input": inp, "output": out, "cache_w": cw, "cache_r": cr,
                    })
        except Exception:
            continue
    return events


def _codex_info(row):
    if isinstance(row.get("info"), dict):
        return row["info"]
    p = row.get("payload")
    if isinstance(p, dict) and isinstance(p.get("info"), dict):
        return p["info"]
    if isinstance(p, dict):
        return p
    return row


def _codex_rate_limits(row):
    """Find a `rate_limits` block regardless of Codex CLI log schema version.

    Older logs nested it inside `info` (so `_codex_info()` happened to surface
    it); current logs put it as a sibling of `info` under `payload`, which
    `_codex_info()` no longer reaches. Check every plausible spot directly
    rather than relying on `_codex_info()`'s single guess.
    """
    if not isinstance(row, dict):
        return None
    for holder in (row, row.get("info"), row.get("payload"),
                   (row.get("payload") or {}).get("info") if isinstance(row.get("payload"), dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("rate_limits"), dict):
            return holder["rate_limits"]
    return None


def parse_codex():
    events = []
    files = []
    for d in CODEX_LOG_DIRS:
        if d.exists():
            files.extend(d.rglob("*.jsonl"))
    for path in set(files):
        try:
            last_cumulative = None
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    info = _codex_info(row)
                    block = field = None
                    for f in ("last_token_usage", "token_usage",
                              "total_token_usage", "usage"):
                        b = info.get(f) if isinstance(info, dict) else None
                        if isinstance(b, dict):
                            block, field = b, f
                            break
                    if not block:
                        continue
                    inp = int(block.get("input_tokens", 0) or 0)
                    out = int(block.get("output_tokens", 0) or 0)
                    cr = int(block.get("cached_input_tokens",
                             block.get("cache_read_input_tokens", 0)) or 0)
                    ts = parse_ts(row.get("timestamp") or row.get("ts"))
                    if field == "total_token_usage":
                        if last_cumulative is None:
                            d_in, d_out, d_cr = inp, out, cr
                        else:
                            d_in = max(0, inp - last_cumulative[0])
                            d_out = max(0, out - last_cumulative[1])
                            d_cr = max(0, cr - last_cumulative[2])
                        last_cumulative = (inp, out, cr)
                        inp, out, cr = d_in, d_out, d_cr
                    if inp == out == cr == 0:
                        continue
                    events.append({
                        "ts": ts, "input": inp, "output": out,
                        "cache_w": 0, "cache_r": cr,
                    })
        except Exception:
            continue
    return events


def _scan_codex_rate_limits():
    latest, latest_ts = None, None
    files = []
    for d in CODEX_LOG_DIRS:
        if d.exists():
            files.extend(d.rglob("*.jsonl"))
    for path in set(files):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    rl = _codex_rate_limits(row)
                    if not isinstance(rl, dict):
                        continue
                    ts = parse_ts(row.get("timestamp") or row.get("ts"))
                    if latest_ts is None or (ts and ts > latest_ts):
                        latest, latest_ts = rl, ts
        except Exception:
            continue
    return latest, latest_ts


def codex_limit_bars(rl):
    """Codex's own logs give an absolute `resets_at` (unix epoch), not a
    countdown, so it's converted to seconds-from-now here. If that moment has
    already passed, the local snapshot is too old to say anything useful about
    the reset, so leave it blank rather than show a misleading "0m"."""
    if not isinstance(rl, dict):
        return []
    out = []
    now = time.time()
    for key in ("primary", "secondary"):
        b = rl.get(key)
        if not isinstance(b, dict):
            continue
        used = b.get("used_percent")
        if used is None:
            continue
        win = b.get("window_minutes") or 0
        label = "Session" if (win and win <= 600) else ("Weekly" if win else key.title())
        secs = b.get("resets_in_seconds")
        if secs is None and b.get("resets_at") is not None:
            try:
                secs = float(b["resets_at"]) - now
            except Exception:
                secs = None
        out.append({"label": label,
                    "percent_left": max(0, min(100, round(100 - float(used)))),
                    "resets": fmt_reset(secs) if (secs is not None and secs >= 0) else None})
    return out


def read_codex_plan():
    """Friendly Codex plan label like 'ChatGPT Plus', read locally from the
    id_token in ~/.codex/auth.json. Only the plan-type claim is used."""
    try:
        data = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
        tok = (data.get("tokens") or {}).get("id_token") or ""
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get("https://api.openai.com/auth") or {}
        plan = (auth.get("chatgpt_plan_type") or "").lower()
    except Exception:
        return None
    label = _CODEX_PLAN_LABELS.get(plan, plan.title() if plan else "")
    return ("ChatGPT " + label) if label else None


_CODEX_PLAN_LABELS = {"plus": "Plus", "pro": "Pro", "team": "Team",
                      "business": "Business", "enterprise": "Enterprise",
                      "edu": "Edu", "free": "Free", "go": "Go"}


def find_codex_binary():
    """Locate the official `codex` executable (needed for the live limits read)."""
    exe = shutil.which("codex")
    if exe:
        return exe
    patterns = []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
        if base:
            patterns.append(os.path.join(base, "OpenAI", "Codex", "bin", "codex.exe"))
    patterns.append(str(Path.home() / ".codex" / "bin" / "codex.exe"))
    for p in patterns:
        if os.path.exists(p):
            return p
    return None


def fetch_codex_usage(timeout=25):
    """Read LIVE Codex rate limits via the official `codex app-server` RPC
    `account/rateLimits/read` — the same call the Codex desktop app makes. This
    is an account read, not a model turn, so it costs no quota. Returns
    (snapshot_in_scan_format, plan_label, error)."""
    exe = find_codex_binary()
    if not exe:
        return None, None, "no-codex"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, **kwargs)
    except Exception:
        return None, None, "spawn-failed"

    result = {"box": None}
    def drive():
        try:
            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"clientInfo": {"name": "ai-usage-monitor",
                                            "title": None, "version": "1.0"},
                             "capabilities": None}})
            asked = False
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == 1 and "result" in msg and not asked:
                    asked = True
                    send({"jsonrpc": "2.0", "id": 2,
                          "method": "account/rateLimits/read"})
                elif msg.get("id") == 2:
                    result["box"] = msg
                    return
        except Exception:
            pass

    t = threading.Thread(target=drive, daemon=True)
    t.start()
    t.join(timeout)
    try:
        proc.terminate()
    except Exception:
        pass

    msg = result["box"]
    if not msg or "result" not in msg:
        return None, None, "no-response"
    rl = (msg["result"] or {}).get("rateLimits") or {}

    def win(w):
        if not isinstance(w, dict):
            return None
        return {"used_percent": w.get("usedPercent"),
                "window_minutes": w.get("windowDurationMins"),
                "resets_at": w.get("resetsAt")}

    snap = {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary"))}
    plan = rl.get("planType")
    plan_label = None
    if plan:
        lbl = _CODEX_PLAN_LABELS.get(str(plan).lower(), str(plan).title())
        plan_label = "ChatGPT " + lbl
    return snap, plan_label, None


# --------------------------------------------------------------------------
# Claude limits via the OAuth usage endpoint
# --------------------------------------------------------------------------

_claude_cache = {"limits": [], "status": "init", "fetched": 0.0}
_claude_lock = threading.Lock()


def read_claude_token():
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip(), "env"
    try:
        data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or data
        tok = oauth.get("accessToken") or oauth.get("access_token")
        if tok:
            return tok, "file"
    except Exception:
        pass
    return None, None


def read_claude_plan():
    """Return a friendly plan label like 'Max (5x)' from the credentials file."""
    try:
        data = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or data
    except Exception:
        return None
    sub = (oauth.get("subscriptionType") or "").lower()
    tier = (oauth.get("rateLimitTier") or "").lower()
    name = {"max": "Max", "pro": "Pro", "team": "Team",
            "enterprise": "Enterprise", "free": "Free"}.get(sub, sub.title() if sub else "")
    mult = ""
    for m in ("20x", "5x", "1x"):
        if m in tier:
            mult = m
            break
    if name and mult:
        return f"{name} ({mult})"
    return name or None


def fetch_claude_usage():
    token, _ = read_claude_token()
    if not token:
        return None, "no-login"
    req = urllib.request.Request(CLAUDE_USAGE_URL, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("anthropic-beta", CLAUDE_BETA)
    req.add_header("User-Agent", CLAUDE_UA)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, {401: "expired", 403: "expired", 429: "rate-limited"}.get(e.code, f"http-{e.code}")
    except Exception:
        return None, "network"


def claude_usage_to_bars(usage):
    out = []
    mapping = [("five_hour", "Session"), ("seven_day", "Weekly"),
               ("seven_day_sonnet", "Weekly (Sonnet)"), ("seven_day_opus", "Weekly (Opus)")]
    for key, label in mapping:
        w = usage.get(key)
        if not isinstance(w, dict):
            continue
        util = w.get("utilization")
        if util is None:
            continue
        secs = None
        if w.get("resets_at"):
            ts = parse_ts(w["resets_at"])
            if ts:
                secs = max(0, (ts - datetime.now(timezone.utc)).total_seconds())
        out.append({"label": label,
                    "percent_left": max(0, min(100, round(100 - float(util)))),
                    "resets": fmt_reset(secs)})
    return out


def refresh_claude_usage():
    usage, err = fetch_claude_usage()
    with _claude_lock:
        _claude_cache["fetched"] = time.time()
        if usage is not None:
            _claude_cache["limits"] = claude_usage_to_bars(usage)
            _claude_cache["status"] = "ok"
        else:
            _claude_cache["status"] = err or "error"
            if err in ("no-login", "expired"):
                _claude_cache["limits"] = []


def claude_usage_loop():
    # Poll the local token file cheaply (every few seconds, no network) and only
    # call the usage endpoint when the token first appears / changes (e.g. right
    # after sign-in or a refresh) or on the normal slow cadence. This keeps us
    # well under the endpoint's rate limit while still showing the bars within a
    # few seconds of the user signing in.
    last_tok = None
    last_fetch = 0.0
    while True:
        tok, _ = read_claude_token()
        now = time.time()
        if (tok and tok != last_tok) or (now - last_fetch >= CLAUDE_POLL_SECONDS):
            try:
                refresh_claude_usage()
            except Exception:
                pass
            last_tok = tok
            last_fetch = now
        time.sleep(5)


def get_claude_limits():
    with _claude_lock:
        return list(_claude_cache["limits"]), _claude_cache["status"]


# --------------------------------------------------------------------------
# one-click sign-in (drives the OFFICIAL `claude` binary, no terminal needed)
# --------------------------------------------------------------------------

def find_claude_binary():
    """Locate a real `claude` executable: PATH first, then the binary that the
    Claude desktop app bundles, then the standard CLI install path."""
    exe = shutil.which("claude")
    if exe:
        return exe
    patterns = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            patterns.append(os.path.join(base, "Claude", "claude-code", "*", "claude.exe"))
    patterns.append(str(Path.home() / ".local" / "bin" / "claude.exe"))
    patterns.append(str(Path.home() / ".local" / "bin" / "claude"))
    found = [c for p in patterns for c in glob.glob(p) if os.path.exists(c)]
    found.sort(key=os.path.getmtime, reverse=True)  # newest version first
    return found[0] if found else None


def start_claude_login():
    """Launch the official `claude auth login` flow in its own window. It opens
    the browser, the user signs in to their own account, and it writes the
    standard credentials file that this app already reads. Returns (ok, error)."""
    exe = find_claude_binary()
    if not exe:
        return False, "no-claude"
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
        subprocess.Popen([exe, "auth", "login"], **kwargs)
        return True, None
    except Exception:
        return False, "spawn-failed"


def run_claude_logout():
    """Sign out via the official `claude auth logout` (clears the local creds).
    Runs hidden and waits, then refreshes so the bars clear immediately."""
    exe = find_claude_binary()
    if not exe:
        return False, "no-claude"
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.run([exe, "auth", "logout"], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        return False, "spawn-failed"
    try:
        refresh_claude_usage()  # reflect the signed-out state without waiting
    except Exception:
        pass
    return True, None


# --------------------------------------------------------------------------
# welcome marker
# --------------------------------------------------------------------------

def is_welcomed():
    try:
        return bool(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("welcomed"))
    except Exception:
        return False


def set_welcomed():
    state = {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state["welcomed"] = True
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Codex limits: live via app-server, with a local-log-snapshot fallback
# --------------------------------------------------------------------------

_codex_cache = {"bars": [], "plan": None, "note": "from Codex CLI logs",
                "hint": "", "fetched": 0.0}
_codex_lock = threading.Lock()


def _codex_from_logs():
    """Fallback view built from the last rate_limits snapshot in the local logs."""
    rl, ts = _scan_codex_rate_limits()
    bars = codex_limit_bars(rl)
    if bars and ts:
        age = fmt_ago((datetime.now(timezone.utc) - ts).total_seconds())
        return bars, f"as of last Codex run · {age}", ""
    if bars:
        return bars, "from Codex CLI logs", ""
    return [], "from Codex CLI logs", "no-data"


def refresh_codex_usage(prefer_live=True):
    bars = note = hint = plan = None
    if prefer_live:
        snap, plan_label, err = fetch_codex_usage()
        if err is None and snap:
            bars = codex_limit_bars(snap)
            if bars:
                note, hint, plan = "live · updated just now", "", plan_label
    if bars is None:                       # no binary / failed → local logs
        bars, note, hint = _codex_from_logs()
    if plan is None:
        plan = read_codex_plan()
    with _codex_lock:
        _codex_cache.update(bars=bars, note=note, hint=hint, plan=plan,
                            fetched=time.time())


def codex_usage_loop():
    # seed instantly from local logs so the card isn't empty while the first
    # (slower) live read spins up, then poll live limits gently.
    try:
        refresh_codex_usage(prefer_live=False)
    except Exception:
        pass
    while True:
        try:
            refresh_codex_usage(prefer_live=True)
        except Exception:
            pass
        time.sleep(CODEX_POLL_SECONDS)


def get_codex_view():
    """(bars, note, hint, plan) for the Codex card. Uses the background cache
    once populated; otherwise computes a quick local-log view synchronously so
    direct callers (tests, first paint) still work without spawning anything."""
    with _codex_lock:
        if _codex_cache["fetched"]:
            c = dict(_codex_cache)
            return c["bars"], c["note"], c["hint"], c["plan"]
    bars, note, hint = _codex_from_logs()
    return bars, note, hint, read_codex_plan()


# --------------------------------------------------------------------------
# build cards
# --------------------------------------------------------------------------

def _usage_rows(events, now):
    today = now.astimezone().date()
    yest = today - timedelta(days=1)
    cutoff30 = now - timedelta(days=30)

    def bucket(pred):
        toks = 0
        for e in events:
            if pred(e["ts"]):
                toks += e["input"] + e["output"] + e["cache_w"] + e["cache_r"]
        return {"tokens": toks}

    return {
        "Today": bucket(lambda ts: ts is not None and ts.astimezone().date() == today),
        "Yesterday": bucket(lambda ts: ts is not None and ts.astimezone().date() == yest),
        "Last 30 Days": bucket(lambda ts: ts is not None and ts >= cutoff30),
    }


def build_cards():
    now = datetime.now(timezone.utc)
    claude = parse_claude()
    codex = parse_codex()
    climits, cstatus = get_claude_limits()

    # Codex bars are read LIVE via the app-server when available (see
    # get_codex_view / codex_usage_loop), falling back to the last local-log
    # snapshot (labelled with its age) when the codex binary isn't installed.
    codex_bars, codex_note, codex_hint, codex_plan = get_codex_view()

    cards = [
        {"name": "Claude", "glyph": "claude", "found": CLAUDE_LOG_DIR.exists(),
         "plan": read_claude_plan(), "signed_in": bool(read_claude_token()[0]),
         "limits": climits, "hint": cstatus, "usage": _usage_rows(claude, now),
         "limit_note": "all Claude apps · chat, Cowork, Code, CLI",
         "token_note": "Claude Code on this PC only"},
        {"name": "Codex CLI", "glyph": "codex",
         "found": any(d.exists() for d in CODEX_LOG_DIRS), "plan": codex_plan,
         "limits": codex_bars, "hint": codex_hint,
         "usage": _usage_rows(codex, now),
         "limit_note": codex_note, "token_note": "this PC only"},
    ]
    with _claude_lock:
        fetched = _claude_cache["fetched"]
    next_secs = int(max(0, fetched + CLAUDE_POLL_SECONDS - time.time())) if fetched else CLAUDE_POLL_SECONDS
    return {"generated": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "claude_next": next_secs,
            "first_run": not is_welcomed(), "cards": cards}


# --------------------------------------------------------------------------
# web view (light-theme widget)
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>AI Usage Monitor</title>
<style>
 :root{--bg:#eceef1;--card:#f5f6f8;--ink:#1f2330;--muted:#8a93a2;
       --reset:#a98b8b;--track:#e2e5ea;--fill:#3b82f6;--line:#e6e8ec}
 *{box-sizing:border-box}
 html,body{overflow:hidden}                  /* widget: never scrolls */
 body{font-family:'Segoe UI',system-ui,Arial,sans-serif;margin:0;
      background:var(--bg);color:var(--ink);font-size:13px}
 .wrap{width:100%;margin:0;padding:6px 10px 8px}   /* card fills width, small side gap */
 .head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
       padding:1px 3px 5px;color:var(--muted);font-size:10px}
 .apptitle{font-weight:700;color:var(--ink);white-space:nowrap}
 .head .when{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:right}
 .prov{margin-bottom:7px}
 .ptitle{display:flex;align-items:center;gap:7px;padding:1px 3px 4px}
 .dots{color:#c2c7cf;font-size:14px;letter-spacing:-2px}
 .pname{font-weight:700;font-size:14px}
 .plan{margin-left:7px;background:#e2e8f5;color:#4b5b78;font-size:10px;
       font-weight:600;padding:2px 7px;border-radius:9px;white-space:nowrap}
 .signout{margin-left:auto;background:var(--fill);color:#fff;border:none;
          font-size:10px;font-weight:600;padding:3px 10px;border-radius:8px;cursor:pointer;
          user-select:none;white-space:nowrap}
 .signout:hover{background:#2f6fe0}
 .card{background:var(--card);border-radius:12px;padding:9px 11px}
 .cap{font-weight:600;font-size:10px;color:var(--muted);text-transform:uppercase;
      letter-spacing:.3px;margin:0 0 4px}
 .cap .sub{font-weight:400;text-transform:none;letter-spacing:0;opacity:.85}
 .limit{margin-bottom:6px}
 .ltitle{font-weight:600;margin-bottom:2px;font-size:12px}
 .bar{height:5px;background:var(--track);border-radius:5px;overflow:hidden}
 .fill{height:100%;background:var(--fill);border-radius:5px}
 .lmeta{display:flex;justify-content:space-between;margin-top:2px;font-size:11px}
 .lreset{color:var(--reset)}
 .sep{height:1px;background:var(--line);margin:6px 0}
 .urow{display:flex;justify-content:space-between;align-items:center;padding:2px 0;font-size:12px}
 .ulabel{font-weight:600;display:flex;align-items:center;gap:5px}
 .uval{color:var(--muted)}
 .note{color:var(--muted);font-size:11px;padding:2px 0 5px}
 .foot{color:var(--muted);font-size:10px;text-align:center;padding-top:4px}
 .welcome{background:var(--card);border-radius:13px;padding:16px 14px}
 .wtitle{font-weight:700;font-size:15px;margin-bottom:8px}
 .welcome p{margin:0 0 10px;line-height:1.4}
 .welcome ul{margin:0 0 12px;padding-left:17px}
 .welcome li{margin-bottom:6px;line-height:1.35}
 .wbtn{display:block;width:100%;background:var(--fill);color:#fff;border:none;
       border-radius:9px;padding:10px 14px;font-size:14px;font-weight:600;cursor:pointer}
 .wbtn:disabled{opacity:.75;cursor:default}
 .cbtn{margin-top:7px;font-size:13px;padding:8px 12px;line-height:1.3}
</style></head><body>
<div class="wrap">
 <div class="head"><span class="apptitle">AI Usage Monitor</span>
   <span class="when">updated <span id="gen">-</span> &middot; next update in <span id="cd">-</span></span></div>
 <div id="root"></div>
 <div class="foot">Limit bars cover all your Claude usage (chat, Cowork, Code, CLI). Token counts are from this PC's logs only.</div>
</div>
<script>
 const ftok=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':''+n;
 const NOTE={'no-login':'Sign in to show your Claude usage limits.',
   'expired':'Claude session expired - click Connect to refresh it.',
   'rate-limited':'Usage check is rate-limited; it will retry shortly.',
   'network':'Could not reach the usage service.',
   'init':'Loading limits...','ok':'No active usage window right now.',
   'no-data':'No usage snapshot yet - run a Codex session, then this fills in.'};
 const LOGINERR={'no-claude':'Claude not found. Install the Claude desktop app, then click Connect again.',
   'spawn-failed':'Could not start sign-in. Please try again.'};
 function welcomeView(){
   return '<div class="welcome"><div class="wtitle">Welcome to AI Usage Monitor</div>'+
     '<p>This app shows how much of your usage limits you have used (as a percentage), plus the tokens used on this computer.</p>'+
     '<ul><li><b>Your data stays on this computer.</b> The only network call is your own usage check to Anthropic, the same one Claude Code makes.</li>'+
     '<li><b>Claude</b> limit bars cover <b>all</b> your Claude usage — chat, Cowork, Claude Code and CLI. Just click <b>Connect Claude</b> the first time to sign in (no terminal needed).</li>'+
     '<li><b>Token</b> counts come from local logs on this PC only, so Claude chat usage is not included in them.</li></ul>'+
     '<button class="wbtn" onclick="dismiss()">Got it</button></div>';
 }
 async function dismiss(){ try{await fetch("/seen");}catch(e){} load(); }
 async function connectClaude(btn){
   btn.disabled=true; btn.textContent='Opening sign-in…';
   try{
     const r=await (await fetch('/login')).json();
     if(r.ok){ btn.textContent='Finish in the window that opened — limits appear here automatically.'; }
     else{ btn.disabled=false; btn.textContent=(LOGINERR[r.error]||'Could not start sign-in. Try again.'); }
   }catch(e){ btn.disabled=false; btn.textContent='Could not start sign-in. Try again.'; }
 }
 async function logoutClaude(el){
   el.textContent='Signing out…';
   try{ await fetch('/logout'); }catch(e){}
   load();
 }
 function card(c){
   let h='<div class="prov"><div class="ptitle"><span class="dots">\u2807\u2807</span>'+
     '<span class="pname">'+c.name+'</span>'+
     (c.plan?('<span class="plan">'+c.plan+'</span>'):'')+
     (c.signed_in?('<span class="signout" onclick="logoutClaude(this)">Sign out</span>'):'')+
     '</div><div class="card">';
   if(!c.found && !(c.hint && c.hint!=='no-login')){
     h+='<div class="note">No logs found yet. Run a session, then this fills in.</div>';
   }
   if(c.limits && c.limits.length){
     h+='<div class="cap">Usage limit'+(c.limit_note?(' <span class="sub">\u00b7 '+c.limit_note+'</span>'):'')+'</div>';
     for(const l of c.limits){
       h+='<div class="limit"><div class="ltitle">'+l.label+'</div>'+
          '<div class="bar"><div class="fill" style="width:'+l.percent_left+'%"></div></div>'+
          '<div class="lmeta"><span>'+l.percent_left+'% left</span>'+
          '<span class="lreset">'+(l.resets?('Resets in '+l.resets):'')+'</span></div></div>';
     }
     h+='<div class="sep"></div>';
   } else if(c.hint){
     h+='<div class="note">'+(NOTE[c.hint]||'Limits unavailable.')+'</div>';
     if(c.glyph==='claude' && (c.hint==='no-login'||c.hint==='expired')){
       h+='<button class="wbtn cbtn" onclick="connectClaude(this)">Connect Claude</button>';
     }
   }
   h+='<div class="cap">Tokens used'+(c.token_note?(' <span class="sub">\u00b7 '+c.token_note+'</span>'):'')+'</div>';
   for(const k of ['Today','Yesterday','Last 30 Days']){
     const u=c.usage[k];if(!u)continue;
     const val=(u.tokens>0)?(ftok(u.tokens)+' tokens'):'\u2014';
     h+='<div class="urow"><span class="ulabel">'+k+'</span>'+
        '<span class="uval">'+val+'</span></div>';
   }
   h+='</div></div>';
   return h;
 }
 let nextSecs=null;
 function fmtCd(s){ if(s==null) return '-'; s=Math.max(0,s);
   const m=Math.floor(s/60), ss=s%60;
   return m>0?(m+'m '+ss+'s'):(ss+'s'); }
 function tickCd(){ if(nextSecs!=null){ nextSecs=Math.max(0,nextSecs-1);
   document.getElementById('cd').textContent=fmtCd(nextSecs); } }
 // Widget sizing: set the window to ~half the screen height ONCE, then scale the
 // content (CSS zoom) so it always fits that fixed height — never scrolls.
 let sized=false;
 function scaleToFit(){
   try{
     document.body.style.zoom='1';
     const avail=window.innerHeight;
     const content=document.documentElement.scrollHeight;
     let z = (content>avail) ? (avail/content) : 1;
     z = Math.max(0.5, Math.min(1, z*0.99));   // small margin; don't shrink to unreadable
     document.body.style.zoom = (z>=0.999 ? '' : z);
   }catch(e){}
 }
 function fitWidget(){
   try{
     const api=window.pywebview&&window.pywebview.api;
     if(api&&api.set_height&&!sized){
       sized=true;
       api.set_height(Math.round(screen.availHeight*__FRACTION__));
       setTimeout(scaleToFit,170);            // let the native resize settle
       return;
     }
     scaleToFit();
   }catch(e){ scaleToFit(); }
 }
 async function load(){
   try{
     const d=await (await fetch('/data')).json();
     document.getElementById('gen').textContent=d.generated;
     if(typeof d.claude_next==='number'){nextSecs=d.claude_next;
       document.getElementById('cd').textContent=fmtCd(nextSecs);}
     if(d.first_run){document.getElementById('root').innerHTML=welcomeView();}
     else{document.getElementById('root').innerHTML=d.cards.map(card).join('');}
     setTimeout(fitWidget,60);
   }catch(e){}
 }
 window.addEventListener('pywebviewready',function(){setTimeout(fitWidget,60);});
 load();setInterval(load,__REFRESH__000);setInterval(tickCd,1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/seen"):
            set_welcomed()
            body, ctype = b'{"ok":true}', "application/json"
        elif self.path.startswith("/login"):
            ok, err = start_claude_login()
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/logout"):
            ok, err = run_claude_logout()
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            ctype = "application/json"
        elif self.path.startswith("/data"):
            body = json.dumps(build_cards()).encode("utf-8")
            ctype = "application/json"
        else:
            body = (PAGE.replace("__REFRESH__", str(REFRESH_SECONDS))
                        .replace("__FRACTION__", str(WINDOW_FRACTION))).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_port(start):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def run_claude_test():
    """Write a diagnostic report for the Claude usage connection."""
    token, src = read_claude_token()
    lines = ["AI Usage Monitor - Claude connection test",
             "=" * 44,
             f"Credentials file : {CLAUDE_CREDS}",
             f"File exists      : {CLAUDE_CREDS.exists()}",
             f"Token found      : {'yes (' + src + ')' if token else 'NO'}",
             f"Endpoint         : {CLAUDE_USAGE_URL}",
             f"User-Agent       : {CLAUDE_UA}", ""]
    if token:
        usage, err = fetch_claude_usage()
        if usage is not None:
            lines.append("RESULT: success. Raw response:")
            lines.append(json.dumps(usage, indent=2))
            lines.append("")
            lines.append("Parsed bars:")
            lines.append(json.dumps(claude_usage_to_bars(usage), indent=2))
        else:
            lines.append(f"RESULT: failed ({err}).")
            if err == "expired":
                lines.append("Your token is stale. Open Claude Code and send a message, then retry.")
            elif err == "rate-limited":
                lines.append("Rate-limited. Wait a few minutes and retry; do not run this repeatedly.")
    else:
        lines.append("RESULT: no token. Run `claude` once and log in, then retry.")
    report = "\n".join(lines)
    out = Path.home() / "usage_monitor_claude_test.txt"
    try:
        out.write_text(report, encoding="utf-8")
    except Exception:
        pass
    print(report)
    try:
        os.startfile(str(out))  # noqa
    except Exception:
        pass


def primary_screen_height():
    """Primary screen height in logical pixels (~CSS px). 0 if it can't be read."""
    try:
        import ctypes
        return int(ctypes.windll.user32.GetSystemMetrics(1))  # SM_CYSCREEN
    except Exception:
        return 0


class WinApi:
    """Exposed to the page so it can set the widget to ~half the screen height.
    The window is non-resizable; the page scales its own content to fit."""

    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def set_height(self, height):
        try:
            h = max(WINDOW_MIN_HEIGHT, min(int(round(float(height))), WINDOW_MAX_HEIGHT))
            if self._window is not None:
                self._window.resize(WINDOW_WIDTH, h)
        except Exception:
            pass
        return True


def main():
    if "--test-claude" in sys.argv:
        run_claude_test()
        return

    threading.Thread(target=claude_usage_loop, daemon=True).start()
    threading.Thread(target=codex_usage_loop, daemon=True).start()

    port = find_port(PREFERRED_PORT)
    url = f"http://localhost:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
    except Exception:
        webview = None
    if webview is not None:
        try:
            sh = primary_screen_height()
            init_h = (max(WINDOW_MIN_HEIGHT, min(int(sh * WINDOW_FRACTION), WINDOW_MAX_HEIGHT))
                      if sh else WINDOW_HEIGHT)
            api = WinApi()
            window = webview.create_window("AI Usage Monitor", url,
                                           width=WINDOW_WIDTH, height=init_h,
                                           resizable=False, on_top=ALWAYS_ON_TOP,
                                           js_api=api)
            api.bind(window)
            webview.start()
            return
        except Exception:
            pass
    print("Opening the dashboard in your browser:", url)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
