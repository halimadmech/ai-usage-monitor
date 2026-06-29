@echo off
REM Run the monitor in its own window now (needs Python). First run installs
REM the small window component automatically.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)
%PY% -m pip install --quiet pywebview >nul 2>nul
%PY% usage_monitor.py
