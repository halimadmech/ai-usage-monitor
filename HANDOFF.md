# HANDOFF — AI MONITOR (system tray + taskbar mini widget)

## Goal
Add two native-app behaviors to the AI Usage Monitor widget, on top of the already-shipped v1.1.0
(live Codex limits):
1. **System tray**: closing the main window minimizes to tray instead of quitting; tray menu has
   Open / Taskbar widget toggle / Lock widget position / Exit. Only Exit truly closes the app.
2. **Taskbar mini widget**: a small dark bar that sits *on* the Windows taskbar (like the user's
   "Awqat Salaat" prayer-time widget), showing `Claude __%` and `Codex __%` (session limit) side by
   side. Hovering it opens a full detail flyout above the bar; moving away collapses it back. It can
   be dragged along the taskbar and locked in place (via tray menu, not an on-widget button).

**Important constraint (already explained to user):** true in-taskbar embedding (Deskband) was
removed in Windows 11. This is instead an always-on-top overlay window sized/positioned to look and
behave like it's part of the taskbar — same technique the user's Awqat Salaat widget uses on Win11.

## Done so far
- System tray implemented: `pystray` + `Pillow` (already installed, added to `build_exe.bat` /
  `START.bat`), `build_tray()`, `MiniController` doubles as tray controller, `request_exit()`,
  hardened `on_closing()` (if tray fails to start, X fully quits instead of hiding forever).
- Taskbar widget went through several iterations with the user testing real screenshots each time:
  1. First version: floating gadget, wrong (user wanted taskbar-docked, Awqat-Salaat style).
  2. Taskbar-docked v1: DPI mismatch caused wrong size/position (pywebview logical px vs Win32
     physical px). Fixed by driving ALL geometry via raw Win32 (`ctypes`) in physical pixels.
  3. Discovered pywebview's `frameless=True` was being ignored by the WebView2 backend — screenshot
     showed a native title bar "AI Usage (mini)" and the window couldn't shrink below a minimum
     size. Fixed: `_style_widget()` strips `WS_CAPTION`/`WS_THICKFRAME`/etc. via
     `SetWindowLongW`/`SetWindowPos` directly, and clears the title via `SetWindowTextW(hwnd, "")`.
  4. Lock button (in-page label) didn't work reliably → **moved lock to the tray menu**
     ("Lock widget position", checkable) — `MiniController.begin_drag()` now reads
     `get_mini_settings()["locked"]` fresh each time, so it's always authoritative.
  5. Codex line missing until hover → root-caused as a **CSS/runtime-measurement race**: the bar's
     height was being measured live via JS (`window.innerHeight`) after Win32 already resized it,
     racing the dock. Fixed: `MINI_BAR_HEIGHT` is now a **fixed constant (40px)** in CSS (no runtime
     measuring at all), window is sized to it × DPI and vertically centered on the taskbar via
     `MiniController._compact_geom()`.
  6. Latest bug (screenshot showed an EMPTY dark box, no text at all): rendered `/mini` in headless
     Edge myself and confirmed **both the compact bar and the expanded panel render correctly with
     real content** — so it wasn't a content bug. Root cause: JS `mouseenter`/`mouseleave` on
     `document.body` is unreliable for a topmost, frameless, always-on-top window — it was getting
     stuck in the "expanded" state (showing the big empty panel because no card data had loaded yet
     into that state, or because it just visually was the wrong div). **Fix just built and shipped
     (not yet confirmed by user):** hover is now driven entirely from **Python**, polling the real
     cursor via `GetCursorPos` in `MiniController.tick()` (called every ~0.2s from
     `taskbar_dock_loop`) — Python resizes the window on hover in/out, and the JS side just reacts
     to `window.innerHeight` to show/hide the panel (`applySize()`, also on a 150ms
     `setInterval` fallback). No more JS mouseenter/leave, no Python↔JS state to desync.
- Verified via headless-Edge screenshots (in scratchpad, not the repo) that both compact and
  expanded states render their content correctly (Claude/Codex %, plan badges, bars, tokens).
- Exe has been rebuilt ~8 times during this iteration; latest is at
  `dist\AIUsage.exe` (built 2026-07-02 00:30, ~20.6 MB, includes pystray+Pillow).

## In progress
- **Waiting on user to test the LATEST rebuild** (cursor-driven hover fix, built 00:30) and confirm:
  1. Both `Claude __%` / `Codex __%` show on the taskbar bar at rest (no hover needed).
  2. Hover opens the flyout panel above the bar; moving away collapses it back to the 2-line bar.
  3. Drag along the taskbar works; tray → "Lock widget position" actually prevents dragging.
  4. No stray "AI Usage (mini)" title anywhere.
- **Nothing has been committed yet.** All of this (tray + taskbar widget, ~8 build iterations) is
  uncommitted working-tree changes on top of commit `5340849` (which is the last pushed state,
  = the public v1.1.0 release). `git status --short` currently shows:
  `M README.md`, `M START.bat`, `M build_exe.bat`, `M usage_monitor.py`.
  (`CLAUDE.md`/`MEMORY.md` also have relevant updates but are git-excluded from the public repo by
  design — see workspace `CLAUDE.md`.)

## Next steps
1. **Get user confirmation** on the current build (`dist\AIUsage.exe`, 00:30) — specifically whether
   the taskbar bar shows both percentages without hovering, and whether hover/drag/lock all work.
2. If something is still off, iterate — **always verify visually before telling the user it's
   fixed**: render `/mini` via headless Edge (pattern below) BEFORE claiming a fix works, since three
   previous "fixed!" claims in this session turned out wrong when the user actually looked.
   Headless-Edge render pattern used repeatedly this session (scratchpad, not repo):
   ```
   sys.path.insert(0, r"D:\AI ZONE\CLAUDE ZONE\GitHub\AI MONITOR")
   import usage_monitor as m
   # start ThreadingHTTPServer(m.Handler) on m.find_port(...), then screenshot
   # http://127.0.0.1:<port>/mini via:
   #   msedge.exe --headless=old --disable-gpu --no-first-run --screenshot=<out>
   #     --hide-scrollbars --window-size=<w>,<h> --force-device-scale-factor=2
   #     --virtual-time-budget=4000 --user-data-dir=<tmp>
   ```
   (This only proves the HTML/CSS/JS renders right — it can't test the real Win32 docking/DPI/hover,
   which genuinely needs the user's machine + screenshots.)
3. Once the user confirms it all works: **commit** (`usage_monitor.py`, `README.md`, `START.bat`,
   `build_exe.bat`) with a message covering both features, **push to `origin main`**, then **cut a
   new GitHub release** (next version after v1.1.0 — this is a real feature add, so v1.2.0) with the
   freshly-built `dist\AIUsage.exe` attached, following the same release script pattern used for
   v1.0.0/v1.1.0 (Python script using `git credential fill` to get a GitHub token in-memory, then
   GitHub API to create the release + upload the asset — see repo's release history / prior turns).
4. Update `CHANGELOG.md` with a v1.2.0 entry (tray + taskbar widget) before/with that release.
5. Consider refreshing README screenshots/copy to mention the tray + taskbar widget (the "Runs like
   a normal app" section already has placeholder copy from an earlier iteration — reread it and
   make sure it matches the FINAL working behavior, e.g. lock is tray-only now, not an on-widget
   button).

## Open questions / decisions
- None outstanding from the user — they've been iterating via screenshot feedback each round. Next
  interaction should just be "does it work now" and then proceed to commit/release per Next steps.
- Not yet decided: exact wording for the v1.2.0 release notes / CHANGELOG entry (draft when ready).

## Key files & how to run
- Main file: `usage_monitor.py` (single-file app). Search for `MiniController`, `taskbar_dock_loop`,
  `PAGE_MINI`, `build_tray`, `_style_widget`, `_tb_rect`, `_compact_geom`, `_expand_geom` for all the
  new code from this session.
- Build: PowerShell (NOT bash) —
  ```
  Get-Process AIUsage -ErrorAction SilentlyContinue | Stop-Process -Force   # onefile leaves 2 procs
  cd "D:\AI ZONE\CLAUDE ZONE\GitHub\AI MONITOR"
  Remove-Item .\build\AIUsage -Recurse -Force -ErrorAction SilentlyContinue
  py -m PyInstaller --onefile --windowed --name AIUsage --icon app.ico --add-data "app.ico;." `
    --hidden-import pystray._win32 --distpath dist --workpath build --specpath . usage_monitor.py
  ```
  (Must kill lingering `AIUsage.exe` processes first or the build fails with a file-lock error —
  happened at least twice this session.)
- Compile-check quickly: `py -m py_compile usage_monitor.py`.
- The user tests by running `dist\AIUsage.exe` directly and sending back a screenshot of the
  taskbar — this has been essential; don't declare victory without one.
- Guide/memory: workspace `CLAUDE.md` has detailed "Gotchas" entries on the Codex log-schema bug
  (separate, already-shipped v1.1.0 fix) and should get a new entry for the taskbar-widget Win32
  approach once this is confirmed working and committed.

## Do NOT redo
- Do NOT re-investigate whether true taskbar (Deskband) embedding is possible — already confirmed
  impossible on Windows 11; the overlay approach is the deliberate, agreed design.
- Do NOT revert to floating-anywhere-on-screen widget — user explicitly wants taskbar-docked.
- Do NOT re-add an on-widget lock button/label — deliberately moved to tray menu because the
  in-page version was unreliable.
- Do NOT re-introduce JS `mouseenter`/`mouseleave` hover detection on the mini window — deliberately
  replaced with Python-side cursor polling (`GetCursorPos` in `MiniController.tick`) because the DOM
  events were unreliable for this frameless/topmost window.
- Do NOT reintroduce runtime height-measuring (`window.innerHeight` read back into layout) for the
  compact bar — deliberately replaced with a fixed `MINI_BAR_HEIGHT` constant to avoid a race.
- The Codex "live limits" feature and the schema-bug fix are DONE and already shipped in v1.1.0
  (committed, pushed, released) — that is NOT part of this handoff's remaining work.
