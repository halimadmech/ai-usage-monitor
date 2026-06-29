"""
AI Usage Monitor
================
A compact desktop widget showing Claude Code and Codex CLI usage in one window:
live limit bars per provider plus estimated cost and tokens for Today,
Yesterday, and the Last 30 Days.

Data sources (all local / your own account):
- Codex limits + tokens : read from Codex CLI's local logs.
- Claude limits         : read from Claude Code's own usage endpoint using the
                          OAuth token that `claude` stores after you log in.
- Claude tokens/cost    : read from Claude Code's local session logs.

Nothing is sent anywhere except your own authenticated request to Anthropic's
usage endpoint, exactly as Claude Code itself does.

Run:           python usage_monitor.py
Build:         see build_exe.bat (standalone UsageMonitor.exe)
Diagnose:      UsageMonitor.exe --test-claude   (writes a report you can read)
"""

import json
import os
import socket
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

WINDOW_WIDTH = 420
WINDOW_HEIGHT = 700
ALWAYS_ON_TOP = False         # True pins the window above others, widget-style

# Claude usage endpoint (the same one Claude Code uses). The User-Agent header
# is REQUIRED; without it the endpoint hard rate-limits. Poll no faster than
# ~180s. Edit CLAUDE_UA if a future Claude Code version rejects this one.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_UA = "claude-code/2.1.114"
CLAUDE_POLL_SECONDS = 300
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"

# Estimated USD per 1,000,000 tokens. ROUGH ESTIMATES for reference only.
PRICING = {
    "opus":   (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0,  15.0,  3.75, 0.30),
    "haiku":  (0.80,  4.0,  1.00, 0.08),
    "gpt-5":  (1.25, 10.0,  1.25, 0.125),
    "codex":  (1.25, 10.0,  1.25, 0.125),
    "o4":     (1.10,  4.4,  1.10, 0.275),
    "_default": (3.0, 15.0, 3.75, 0.30),
}

CLAUDE_LOG_DIR = Path.home() / ".claude" / "projects"
CODEX_LOG_DIRS = [Path.home() / ".codex" / "sessions", Path.home() / ".codex"]
STATE_FILE = Path.home() / ".usage_monitor_state.json"  # welcome marker only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def price_for(model):
    if not model:
        return PRICING["_default"]
    m = model.lower()
    for key, rates in PRICING.items():
        if key != "_default" and key in m:
            return rates
    return PRICING["_default"]


def cost_of(model, inp, out, cw, cr):
    pin, pout, pcw, pcr = price_for(model)
    return (inp * pin + out * pout + cw * pcw + cr * pcr) / 1_000_000.0


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


# --------------------------------------------------------------------------
# local log parsers (tokens + cost rows; Codex limits)
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
                    model = msg.get("model") or row.get("model")
                    events.append({
                        "ts": parse_ts(row.get("timestamp")),
                        "input": inp, "output": out, "cache_w": cw, "cache_r": cr,
                        "cost": cost_of(model, inp, out, cw, cr),
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
                    model = (row.get("model")
                             or (info.get("model") if isinstance(info, dict) else None)
                             or "codex")
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
                        "cost": cost_of(model, inp, out, 0, cr),
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
                    info = _codex_info(row)
                    rl = info.get("rate_limits") if isinstance(info, dict) else None
                    if not isinstance(rl, dict):
                        continue
                    ts = parse_ts(row.get("timestamp") or row.get("ts"))
                    if latest_ts is None or (ts and ts > latest_ts):
                        latest, latest_ts = rl, ts
        except Exception:
            continue
    return latest


def codex_limit_bars(rl):
    if not isinstance(rl, dict):
        return []
    out = []
    for key in ("primary", "secondary"):
        b = rl.get(key)
        if not isinstance(b, dict):
            continue
        used = b.get("used_percent")
        if used is None:
            continue
        win = b.get("window_minutes") or 0
        label = "Session" if (win and win <= 600) else ("Weekly" if win else key.title())
        out.append({"label": label,
                    "percent_left": max(0, min(100, round(100 - float(used)))),
                    "resets": fmt_reset(b.get("resets_in_seconds"))})
    return out


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
    while True:
        try:
            refresh_claude_usage()
        except Exception:
            pass
        time.sleep(CLAUDE_POLL_SECONDS)


def get_claude_limits():
    with _claude_lock:
        return list(_claude_cache["limits"]), _claude_cache["status"]


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
# build cards
# --------------------------------------------------------------------------

def _usage_rows(events, now):
    today = now.astimezone().date()
    yest = today - timedelta(days=1)
    cutoff30 = now - timedelta(days=30)

    def bucket(pred):
        cost, toks = 0.0, 0
        for e in events:
            if pred(e["ts"]):
                cost += e["cost"]
                toks += e["input"] + e["output"] + e["cache_w"] + e["cache_r"]
        return {"cost": round(cost, 2), "tokens": toks}

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

    cards = [
        {"name": "Claude Code", "glyph": "claude", "found": CLAUDE_LOG_DIR.exists(),
         "plan": read_claude_plan(),
         "limits": climits, "hint": cstatus, "usage": _usage_rows(claude, now)},
        {"name": "Codex CLI", "glyph": "codex",
         "found": any(d.exists() for d in CODEX_LOG_DIRS), "plan": "",
         "limits": codex_limit_bars(_scan_codex_rate_limits()), "hint": "",
         "usage": _usage_rows(codex, now)},
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
 body{font-family:'Segoe UI',system-ui,Arial,sans-serif;margin:0;
      background:var(--bg);color:var(--ink);font-size:14px}
 .wrap{max-width:430px;margin:0 auto;padding:8px 12px 12px}
 .head{display:flex;justify-content:space-between;align-items:center;
       padding:2px 4px 7px;color:var(--muted);font-size:11px}
 .prov{margin-bottom:9px}
 .ptitle{display:flex;align-items:center;gap:8px;padding:1px 4px 5px}
 .dots{color:#c2c7cf;font-size:15px;letter-spacing:-2px}
 .pname{font-weight:700;font-size:15px}
 .plan{margin-left:8px;background:#e2e8f5;color:#4b5b78;font-size:11px;
       font-weight:600;padding:2px 8px;border-radius:10px}
 .card{background:var(--card);border-radius:13px;padding:11px 13px}
 .limit{margin-bottom:8px}
 .ltitle{font-weight:600;margin-bottom:3px;font-size:13px}
 .bar{height:6px;background:var(--track);border-radius:6px;overflow:hidden}
 .fill{height:100%;background:var(--fill);border-radius:6px}
 .lmeta{display:flex;justify-content:space-between;margin-top:3px;font-size:12px}
 .lreset{color:var(--reset)}
 .sep{height:1px;background:var(--line);margin:8px 0}
 .urow{display:flex;justify-content:space-between;align-items:center;padding:3px 0;font-size:13px}
 .ulabel{font-weight:600;display:flex;align-items:center;gap:5px}
 .i{display:inline-block;width:13px;height:13px;border:1px solid #c2c7cf;
    border-radius:50%;color:#aeb4bd;font-size:9px;line-height:13px;text-align:center}
 .uval{color:var(--muted)}
 .note{color:var(--muted);font-size:12px;padding:3px 0 6px}
 .foot{color:var(--muted);font-size:11px;text-align:center;padding-top:5px}
 .welcome{background:var(--card);border-radius:14px;padding:18px 16px}
 .wtitle{font-weight:700;font-size:16px;margin-bottom:9px}
 .welcome p{margin:0 0 11px;line-height:1.45}
 .welcome ul{margin:0 0 14px;padding-left:18px}
 .welcome li{margin-bottom:7px;line-height:1.4}
 .wbtn{display:block;width:100%;background:var(--fill);color:#fff;border:none;
       border-radius:10px;padding:11px 16px;font-size:15px;font-weight:600;cursor:pointer}
</style></head><body>
<div class="wrap">
 <div class="head"><span>AI Usage Monitor</span>
   <span>updated <span id="gen">-</span> &middot; next update in <span id="cd">-</span></span></div>
 <div id="root"></div>
 <div class="foot">Costs are rough estimates. Claude limits come from your Claude Code login.</div>
</div>
<script>
 const ftok=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':''+n;
 const fcost=c=>c>=1000?'$'+(c/1000).toFixed(1)+'K':'$'+c.toFixed(2);
 const NOTE={'no-login':'Log in to Claude Code to show limits.',
   'expired':'Claude session expired - open Claude Code to refresh.',
   'rate-limited':'Usage check is rate-limited; it will retry shortly.',
   'network':'Could not reach the usage service.',
   'init':'Loading limits...','ok':'No active usage window right now.'};
 function welcomeView(){
   return '<div class="welcome"><div class="wtitle">Welcome to AI Usage Monitor</div>'+
     '<p>This app shows how much you have used Claude Code and Codex, with rough cost estimates.</p>'+
     '<ul><li><b>Your data stays on this computer.</b> The only network call is your own usage check to Anthropic, the same one Claude Code makes.</li>'+
     '<li><b>Codex</b> usage shows up right away.</li>'+
     '<li><b>Claude</b> limits appear once you have logged in to Claude Code at least once.</li></ul>'+
     '<button class="wbtn" onclick="dismiss()">Got it</button></div>';
 }
 async function dismiss(){ try{await fetch("/seen");}catch(e){} load(); }
 function card(c){
   let h='<div class="prov"><div class="ptitle"><span class="dots">\u2807\u2807</span>'+
     '<span class="pname">'+c.name+'</span>'+
     (c.plan?('<span class="plan">'+c.plan+'</span>'):'')+'</div><div class="card">';
   if(!c.found && !(c.hint && c.hint!=='no-login')){
     h+='<div class="note">No logs found yet. Run a session, then this fills in.</div>';
   }
   if(c.limits && c.limits.length){
     for(const l of c.limits){
       h+='<div class="limit"><div class="ltitle">'+l.label+'</div>'+
          '<div class="bar"><div class="fill" style="width:'+l.percent_left+'%"></div></div>'+
          '<div class="lmeta"><span>'+l.percent_left+'% left</span>'+
          '<span class="lreset">'+(l.resets?('Resets in '+l.resets):'')+'</span></div></div>';
     }
     h+='<div class="sep"></div>';
   } else if(c.hint){
     h+='<div class="note">'+(NOTE[c.hint]||'Limits unavailable.')+'</div>';
   }
   for(const k of ['Today','Yesterday','Last 30 Days']){
     const u=c.usage[k];if(!u)continue;
     const val=(u.tokens>0)?(fcost(u.cost)+' \u00b7 '+ftok(u.tokens)+' tokens'):'\u2014';
     h+='<div class="urow"><span class="ulabel">'+k+' <span class="i">i</span></span>'+
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
 async function load(){
   try{
     const d=await (await fetch('/data')).json();
     document.getElementById('gen').textContent=d.generated;
     if(typeof d.claude_next==='number'){nextSecs=d.claude_next;
       document.getElementById('cd').textContent=fmtCd(nextSecs);}
     if(d.first_run){document.getElementById('root').innerHTML=welcomeView();return;}
     document.getElementById('root').innerHTML=d.cards.map(card).join('');
   }catch(e){}
 }
 load();setInterval(load,__REFRESH__000);setInterval(tickCd,1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/seen"):
            set_welcomed()
            body, ctype = b'{"ok":true}', "application/json"
        elif self.path.startswith("/data"):
            body = json.dumps(build_cards()).encode("utf-8")
            ctype = "application/json"
        else:
            body = PAGE.replace("__REFRESH__", str(REFRESH_SECONDS)).encode("utf-8")
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


def main():
    if "--test-claude" in sys.argv:
        run_claude_test()
        return

    threading.Thread(target=claude_usage_loop, daemon=True).start()

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
            webview.create_window("AI Usage Monitor", url,
                                  width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
                                  min_size=(360, 420), on_top=ALWAYS_ON_TOP)
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
