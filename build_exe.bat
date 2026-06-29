@echo off
REM ============================================================
REM   Build UsageMonitor.exe  --  just double-click this file.
REM ============================================================
setlocal
cd /d "%~dp0"
echo.
echo  ============================================================
echo    Building UsageMonitor.exe
echo  ============================================================
echo.

REM --- find Python ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo  [ERROR] Python was not found on this PC.
  echo.
  echo  Install Python 3 first from https://www.python.org/downloads/
  echo  On the first install screen, TICK "Add python.exe to PATH".
  echo  Then run this file again.
  echo.
  pause
  exit /b 1
)
echo  Using Python: %PY%
echo.

echo  Step 1 of 2: installing the build tools...
%PY% -m pip install --quiet --upgrade pip pyinstaller pywebview
if errorlevel 1 (
  echo  [ERROR] Could not install the build tools. Check your internet connection.
  pause
  exit /b 1
)
echo  Done.
echo.

echo  Step 2 of 2: compiling the executable...
%PY% -m PyInstaller --onefile --windowed --name AIUsage --icon app.ico usage_monitor.py
if errorlevel 1 (
  echo  [ERROR] The build failed. See the messages above.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo    SUCCESS
echo  ============================================================
echo    Your program is here:
echo        %CD%\dist\AIUsage.exe
echo.
echo    Double-click that .exe any time to open your usage dashboard.
echo  ============================================================
echo.
pause
