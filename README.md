# AI Usage Monitor (Claude + Codex)

One compact desktop widget showing **Claude Code** and **Codex CLI** usage
together: live limit bars per provider, plus estimated cost and tokens for
Today, Yesterday, and the Last 30 Days. It opens in its own window.

## What you need

- **Claude Code installed and logged in.** The Claude limit bars read the login
  token that `claude` saves after you sign in. If you have only the desktop app,
  install the CLI once: in PowerShell run `irm https://claude.ai/install.ps1 | iex`,
  open a new PowerShell, run `claude`, and sign in.
- **Python 3** (to build the exe): https://www.python.org/downloads/ , tick
  "Add python.exe to PATH".

Codex usage works with no login; it is read straight from Codex's local logs.

## Build and run

1. Double-click **build_exe.bat**.
2. Run **dist\AIUsage.exe**. It opens in its own window.

There is nothing to configure. No settings files are edited.

## How the data is sourced

- **Codex** limits and tokens come from Codex CLI's local logs.
- **Claude** limits come from Claude Code's own usage endpoint, called with the
  OAuth token `claude` stores in `~/.claude/.credentials.json` — the same
  request Claude Code makes for its `/usage` screen. It is polled gently (every
  5 minutes) to respect the endpoint's limits.
- **Claude** tokens and cost come from Claude Code's local session logs.

The Claude token expires about hourly and refreshes whenever Claude Code runs.
If the card says "session expired", open Claude Code briefly and it refreshes.

## If the Claude bars do not appear

Run a diagnostic: open PowerShell in the folder and run
`dist\AIUsage.exe --test-claude`. It writes **usage_monitor_claude_test.txt**
to your user folder (and opens it) showing whether the token was found, the HTTP
result, and the raw response. Share that file if you need help; it contains no
secrets beyond what is needed to diagnose.

## Make it a floating widget

In `usage_monitor.py`, set `ALWAYS_ON_TOP = True` and adjust `WINDOW_WIDTH` /
`WINDOW_HEIGHT`, then rebuild.

## Notes

- The window uses the **Edge WebView2** runtime included with Windows 11; if it
  is ever blank it falls back to your browser.
- Cost figures are **rough estimates** from the price table near the top of
  `usage_monitor.py`.
- Requires a Claude.ai **Pro, Max, or Team** subscription for the Claude limit
  data (it is tied to subscription login, not API keys).

## Why are Today / Yesterday sometimes blank?

The limit **bars** come from Anthropic's server and count all your usage
everywhere (desktop app, website, CLI). The **cost and token rows** are totalled
only from Claude Code's and Codex's own session logs on this computer. If your
recent usage was in the desktop app's chat or on claude.ai, it is not written to
those local logs, so Today/Yesterday can read "—" even while the bars move. Run
an actual Claude Code or Codex session and the rows fill in.

## Icon and refresh countdown

The app now ships with its own icon (`app.ico`, used automatically by
`build_exe.bat`). The header shows "next update in …", counting down to the next
Claude limit refresh (every 5 minutes).
